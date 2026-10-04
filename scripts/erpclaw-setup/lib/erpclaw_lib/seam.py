"""The database seam — schema, connections and catalog, one way in (ADR-0034).

Every question of the form "how do I make a table", "how do I open a database" or
"what does this database contain" is answered here and nowhere else. SQLAlchemy
Core provides the machinery; it is an implementation detail of this module.

**PyPika still builds every query.** ADR-0034 §5b(i) is explicit and load-bearing:
DML call sites keep receiving DBAPI-compatible connections with the same
``.execute(sql, params)`` / ``.commit()`` contract they have today, so no query
is rewritten. SQLAlchemy ``Engine`` objects live inside this module for DDL,
pooling and introspection; a SQLAlchemy ``Connection`` reaching a DML call site
would be Option C by the back door and needs a superseding ADR.

**Why this module exists at all.** The live-PostgreSQL gate measured 40 module
installers hardcoding ``sqlite3.connect``, 31 ``sqlite_master`` reads, and 67
test conftests unable to observe PostgreSQL at all — so "PostgreSQL is
supported" was true of the foundation and false of every module on top of it.
Hand-maintained dialect branches were the regime that produced that drift. One
seam plus an enforcing gate is the replacement.

**Import cost.** ``erpclaw_lib.db`` is imported by every action on every
invocation; SQLAlchemy is not cheap to import. Nothing here is imported at
module scope by the DML path — the vendored tree is put on ``sys.path`` and
imported lazily, on first use, by callers that actually provision or introspect.

Money discipline is unchanged and enforced by the type map: money columns are
TEXT on every backend, holding exact ``Decimal`` strings. Never float, never
NUMERIC — the invariant tier compares those strings exactly, and NUMERIC's
trailing-zero and equality semantics would bend that silently.

**Transaction boundary, for whoever writes phase 2.** A seam engine opens its
own connection, distinct from the one ``get_connection`` hands the caller. DDL
issued here is therefore NOT inside the caller's transaction and will not roll
back with it. That is unavoidable — SQLite gives DDL its own semantics and
PostgreSQL takes different locks for it — but it has consequences worth planning
around rather than discovering: provisioning is not atomic with the seeding that
follows it, and a module provisioning while another connection holds a write
transaction will wait on the lock (5s on both backends, then raise, rather than
hanging). Provision first, commit, then open the DML connection.

Supported backends are SQLite and PostgreSQL. MySQL was ruled out on 2026-08-11:
ERPClaw keys and indexes TEXT columns, and MySQL cannot key or index TEXT
without a prefix length.
"""
import os
import sys
import threading

from erpclaw_lib.db import (
    get_dialect, DEFAULT_DB_PATH, _resolve_pg_url, ensure_db_exists,
    setup_pragmas, _DecimalSum, db_error_types, readonly_requested,
    _reject_url_shaped_path, readonly_sqlite_uri,
)

_VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")

_engines = {}
_engine_lock = threading.Lock()


def _sqlalchemy():
    """Import the vendored SQLAlchemy, lazily and from our own tree.

    SQLAlchemy imports its own submodules by absolute name (``sqlalchemy.sql``
    …), including through a string-keyed preloader, so — unlike PyPika, whose
    internal imports are relative — it cannot simply be nested under
    ``erpclaw_lib.vendor``. The vendor directory goes on ``sys.path`` instead and
    the package is imported top-level.

    Ours is put FIRST deliberately. ERPClaw actions run as their own processes,
    so the blast radius is our own interpreter, and a product that must behave
    identically on every machine cannot have its schema layer silently swapped
    for whatever version happens to be installed on the host.
    """
    if _VENDOR not in sys.path:
        sys.path.insert(0, _VENDOR)
    import sqlalchemy

    # sys.path order decides nothing if something already imported SQLAlchemy —
    # sys.modules short-circuits the lookup and we silently inherit whatever
    # version that was. For a query layer that would be tolerable; for the layer
    # that emits CREATE TABLE it is the exact nondeterminism ADR-0034 exists to
    # end, so fail loudly rather than provision from an unknown version.
    loaded = os.path.abspath(getattr(sqlalchemy, "__file__", "") or "")
    if not loaded.startswith(os.path.abspath(_VENDOR) + os.sep):
        raise RuntimeError(
            "erpclaw_lib.seam requires the vendored SQLAlchemy, but "
            f"'sqlalchemy' was already imported from {loaded or '<unknown>'}. "
            "ERPClaw emits schema DDL through this module; provisioning from an "
            "unpinned version is not safe. Import erpclaw_lib.seam before any "
            "other SQLAlchemy user, or run ERPClaw in its own process.")
    return sqlalchemy


def sqlalchemy_url(db_path=None) -> str:
    """The active database as a SQLAlchemy URL, using the same env chain as DML.

    Deliberately delegates to ``db._resolve_pg_url`` rather than re-reading the
    environment: two independent resolutions of "which database" is precisely
    the class of bug ADR-0034 exists to end.
    """
    if get_dialect() == "postgresql":
        url = _resolve_pg_url(db_path)
        # psycopg2 is the driver on both sides; make the driver explicit so the
        # engine can never pick a different one than the DML path uses.
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg2://" + url[len("postgresql://"):]
        return url
    raw = db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)
    _reject_url_shaped_path(raw)
    path = os.path.abspath(os.path.expanduser(raw))
    if readonly_requested():
        # Read-only storage creates nothing: no parent directory, no file.
        # The engine opens the existing file through a mode=ro URI, the same
        # driver-level refusal get_connection uses.
        if not os.path.isfile(path):
            raise FileNotFoundError(
                "database file does not exist and ERPCLAW_DB_READONLY=1 "
                "refuses to create it: %s" % path
            )
        return "sqlite:///" + readonly_sqlite_uri(path) + "&uri=true"
    # Same courtesy get_connection extends: create the parent directory. Phase 2
    # provisions databases through this path, and SQLite reports a missing parent
    # as the thoroughly unhelpful "unable to open database file".
    ensure_db_exists(path)
    # Percent-encoded file: URI, as in read-only mode, so a "?", "#" or "%"
    # in the path cannot become URL syntax.
    from urllib.parse import quote as _quote
    return "sqlite:///file:" + _quote(path, safe="/") + "?uri=true"


def get_engine(db_path=None):
    """A SQLAlchemy Engine for DDL and introspection. INTERNAL to the seam.

    Do not hand the result, or anything derived from it, to code that runs
    queries — see the module docstring. Engines are cached per resolved URL
    because building one parses the URL and loads a dialect module.
    """
    sa = _sqlalchemy()
    url = sqlalchemy_url(db_path)
    with _engine_lock:
        engine = _engines.get(url)
        if engine is None:
            # NullPool: ERPClaw actions are short-lived processes that run one
            # command and exit. A QueuePool would hold an idle server connection
            # open for the life of every one of them — real pressure on a
            # PostgreSQL server's connection limit when many actions run at
            # once, for pooling nobody collects on. Connect-per-use is correct
            # for this shape, and DDL and introspection are not hot paths.
            engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
            _match_dml_connection_settings(sa, engine)
            _engines[url] = engine
        return engine


def _match_dml_connection_settings(sa, engine):
    """Give seam connections the settings ``get_connection`` gives DML ones.

    Calls the SAME ``setup_pragmas`` the DML path calls, rather than a second
    copy that drifts: WAL / foreign keys / busy_timeout on SQLite, and
    lock_timeout / statement_timeout on PostgreSQL.

    The PostgreSQL half matters more here than it does for DML. This engine is
    what emits DDL, and DDL takes far stronger locks than a SELECT does — so
    without a lock_timeout a CREATE TABLE waiting behind an open transaction
    waits forever. The path most in need of a timeout was the one that had none.

    ``decimal_sum`` is registered too, and only on SQLite, because there it is a
    per-connection Python aggregate while on PostgreSQL it is a real server-side
    function that every connection already sees. That asymmetry is exactly the
    kind that survives review: identical code, works on one backend, fails on
    the other. Phase 4 moves the invariant engine onto this seam and would have
    met it as "invariants pass on PostgreSQL, fail on SQLite".
    """
    @sa.event.listens_for(engine, "connect")
    def _configure(dbapi_conn, _record):  # pragma: no cover - event hook
        setup_pragmas(dbapi_conn)
        if get_dialect() == "sqlite":
            dbapi_conn.create_aggregate("decimal_sum", 1, _DecimalSum)


# ── Module schema declaration (ADR-0034 phase 2) ─────────────────────────────
#
# Modules declare tables here and never import SQLAlchemy themselves. That is
# not politeness: 40 modules importing SQLAlchemy directly would be 40 new import
# sites for the bypass gate to police, and the seam would stop being a seam. The
# names below are resolved lazily through the module __getattr__ at the bottom,
# so `import erpclaw_lib.seam` stays cheap for the DML path.

_DECLARATION_NAMES = {
    # structure
    "MetaData", "Table", "Column", "Index", "CheckConstraint",
    "ForeignKey", "ForeignKeyConstraint", "UniqueConstraint",
    "PrimaryKeyConstraint", "text",
    # the only column types ERPClaw declares. Money and IDs are TEXT on every
    # backend (ADR-0034 dec. 1); Integer is for counts and boolean-ish flags.
    "Text", "Integer",
}


def __getattr__(name):
    """Lazily expose SQLAlchemy's declaration vocabulary (PEP 562).

    Keeps SQLAlchemy off the import path of anything that only wanted
    `get_connection`, while letting a module write
    ``from erpclaw_lib.seam import Table, Column, Text``.
    """
    if name in _DECLARATION_NAMES:
        return getattr(_sqlalchemy(), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_REFERENCE_ONLY = "erpclaw_reference_only"
_now_default_cls = None


def now_default():
    """A column DEFAULT meaning "now", spelled correctly for each backend.

    53 columns across 5 installers ship ``DEFAULT (datetime('now'))``. That is
    SQLite's spelling; PostgreSQL has no ``datetime()`` function and rejects the
    DDL outright::

        UndefinedFunction: function datetime(unknown) does not exist

    which put ADR-0034 phase 2 in a bind of its own making. The conversion's
    merge bar is diff-to-zero, and defaults are compared character for
    character, so rewriting the default to ``CURRENT_TIMESTAMP`` fails the proof
    on SQLite; transcribing it faithfully fails the phase's actual objective,
    which is that the module provisions on PostgreSQL. Both honest options were
    wrong.

    The seam exists precisely to end that trade: it emits dialect-correct DDL, so
    the default renders as ``(datetime('now'))`` on SQLite — byte-identical to
    what shipped, so parity still proves — and as ``CURRENT_TIMESTAMP`` on
    PostgreSQL, which means the same thing there. Neither backend sees a
    compromise.

    Behaviourally identical on SQLite: ``datetime('now')`` and
    ``CURRENT_TIMESTAMP`` both yield UTC ``YYYY-MM-DD HH:MM:SS``.
    """
    global _now_default_cls
    if _now_default_cls is None:
        sa = _sqlalchemy()
        from sqlalchemy.ext.compiler import compiles

        class _ErpclawNow(sa.sql.expression.ColumnElement):
            inherit_cache = True

        @compiles(_ErpclawNow)
        def _render_default(element, compiler, **kw):  # noqa: ARG001
            return "(datetime('now'))"

        @compiles(_ErpclawNow, "postgresql")
        def _render_postgresql(element, compiler, **kw):  # noqa: ARG001
            return "CURRENT_TIMESTAMP"

        _now_default_cls = _ErpclawNow
    return _now_default_cls()


def reference_table(name, metadata, pk="id", pk_type=None):
    """Declare a table this module does NOT own, so its foreign keys resolve.

    SQLAlchemy resolves ``ForeignKey("company.id")`` inside the declaring
    ``MetaData`` and raises ``NoReferencedTableError`` when the target is absent.
    Nearly every ERPClaw module points at tables another module owns — measured
    across the 40 installers: **623 such references in 38 of them**, 405 of those
    to ``company`` alone — so this is the ordinary case, not an exception.

    The two obvious answers are both wrong. Dropping the foreign key to make the
    declaration compile silently discards a real integrity constraint the raw DDL
    had. Declaring the target as a normal ``Table`` makes ``provision`` CREATE
    another module's table, which the ownership rule forbids outright.

    So the target is declared for resolution only and excluded from creation:
    the emitted DDL still carries ``REFERENCES company(id)``, and ``company``
    itself is never touched by this module.

    Only the primary key is declared. This is not a description of the other
    module's table and must never be treated as one — it exists so a foreign key
    has something to point at.
    """
    sa = _sqlalchemy()
    if name in metadata.tables:
        return metadata.tables[name]
    return sa.Table(
        name, metadata,
        sa.Column(pk, pk_type or sa.Text, primary_key=True),
        info={_REFERENCE_ONLY: True},
    )


def provision(metadata, db_path=None):
    """Create every table and index in `metadata` that does not already exist.

    The dialect-correct replacement for a module's hand-written
    ``CREATE TABLE IF NOT EXISTS`` block. Idempotent by the same contract:
    ``checkfirst`` skips what is already there, so a re-run creates nothing.

    Returns ``{"database", "tables", "indexes"}`` with counts of what was
    ACTUALLY created, measured as a before/after delta rather than taken from
    SQLAlchemy — the same honest mechanism `module_manager` uses for
    ``tables_created`` (F11, ADR-0029). `create_all` reports nothing about what
    it skipped, and a count that quietly includes pre-existing tables is the
    exact dishonesty F11 was about.
    """
    if readonly_requested():
        raise RuntimeError("provisioning refused: ERPCLAW_DB_READONLY=1")
    engine = get_engine(db_path)
    # Reference-only declarations exist so foreign keys resolve; creating them
    # would mean this module provisioning another module's table.
    owned = [t for t in metadata.sorted_tables
             if not t.info.get(_REFERENCE_ONLY)]
    declared = [t.name for t in owned]

    def _snapshot():
        existing = set(table_names(db_path))
        idx = set()
        for t in declared:
            if t in existing:
                idx.update(f"{t}.{i}" for i in index_names(t, db_path))
        return existing & set(declared), idx

    tables_before, idx_before = _snapshot()
    metadata.create_all(engine, tables=owned, checkfirst=True)
    tables_after, idx_after = _snapshot()

    return {
        "database": sqlalchemy_url(db_path),
        "tables": len(tables_after - tables_before),
        "indexes": len(idx_after - idx_before),
    }


def declared_schema_in_source(path):
    """The full schema a source file DECLARES as metadata, without executing it.

    Returns ``{table: {"columns": [{"name", "type", "is_pk"}], "indexes": [...]}}``
    — the shape `schema_diff` already uses for text-parsed DDL, so a converted
    module drops straight into its declared-vs-live comparison.

    Static by the same reasoning as `declared_tables_in_source`: importing 40
    module files to ask what they declare would run their import side effects and
    make every governance instrument dependent on every module being importable.

    Carries the same two guards — the file must reference ``erpclaw_lib.seam``,
    and a `Table(...)` call must pass a second positional argument — because
    PyPika spells its query tables the same way.
    """
    import ast

    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            source = fh.read()
        tree = ast.parse(source, str(path))
    except (OSError, SyntaxError):
        return {}

    if "erpclaw_lib.seam" not in source:
        return {}

    def _call_name(node):
        return getattr(node.func, "id", None) or getattr(node.func, "attr", None)

    def _first_str(node):
        if node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            return node.args[0].value
        return None

    schema = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) != "Table":
            continue
        if len(node.args) < 2:
            continue  # PyPika query table, not a declaration
        table = _first_str(node)
        if not table:
            continue
        columns, indexes = [], []
        for arg in node.args[2:]:
            if not isinstance(arg, ast.Call):
                continue
            kind = _call_name(arg)
            if kind == "Column":
                name = _first_str(arg)
                if not name:
                    continue
                ctype = None
                if len(arg.args) > 1:
                    a1 = arg.args[1]
                    ctype = getattr(a1, "id", None) or getattr(a1, "attr", None) \
                        or _call_name(a1) if isinstance(a1, ast.Call) else \
                        getattr(a1, "id", None) or getattr(a1, "attr", None)
                is_pk = any(k.arg == "primary_key"
                            and isinstance(k.value, ast.Constant)
                            and k.value.value is True
                            for k in arg.keywords)
                columns.append({"name": name, "type": (ctype or "TEXT").upper(),
                                "is_pk": is_pk})
            elif kind == "Index":
                iname = _first_str(arg)
                if iname:
                    indexes.append(iname)
        schema[table] = {"columns": columns, "indexes": indexes}

    # A module-level `Index("ix", SOME_TABLE.c.col)` names its table through the
    # Python variable the Table was assigned to, so resolving it needs the
    # variable→table map that only the assignment statements carry.
    var_to_table = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        if _call_name(node.value) != "Table" or len(node.value.args) < 2:
            continue
        tname = _first_str(node.value)
        if not tname:
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name):
                var_to_table[tgt.id] = tname

    # Index(...) declared outside the Table(...) call, the common SQLAlchemy form.
    #
    # This attributed every such index to whichever table the walk reached first
    # (ADR-0034 step 2f). It resolved the owning variable and then discarded it:
    # `for tname, tdef in schema.items(): ...; break`. On `erpclaw-esign` all 9
    # indexes landed on `esign_signature_request` and `esign_signature_event`
    # reported none, so the declared-vs-live comparison — and `schema_migrator`,
    # which shares this reader — saw 4 phantom indexes on one table and 4 missing
    # from another. Harmless-looking with one converted module; phase 2 converts
    # 40, and every index a module declares outside its Table call would have
    # been mis-filed the same way.
    #
    # Blind spot, stated per D2: only the `TABLE.c.column` form is resolvable
    # statically. An index whose target is a bare string column name, or a table
    # held in a list/dict rather than a plain variable, is left unattributed
    # rather than guessed — a wrong owner reads as drift on two tables at once,
    # which is worse than a known omission.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) != "Index":
            continue
        iname = _first_str(node)
        if not iname:
            continue
        for arg in node.args[1:]:
            if not isinstance(arg, ast.Attribute):       # t.c.column
                continue
            inner = arg.value
            if not (isinstance(inner, ast.Attribute) and inner.attr == "c"):
                continue
            table = var_to_table.get(getattr(inner.value, "id", None))
            if table and table in schema:
                if iname not in schema[table]["indexes"]:
                    schema[table]["indexes"].append(iname)
                break
    return schema


def declared_tables_in_source(path):
    """Table names a source file DECLARES as metadata, without executing it.

    Delegates to `declared_schema_in_source` rather than re-walking the AST — two
    readers with two copies of the PyPika/SQLAlchemy guards is precisely the
    drift this module exists to prevent.
    """
    return list(declared_schema_in_source(path))


def error_types():
    """Exception classes anything in this module can raise, as an except-tuple.

    Callers must not have to import SQLAlchemy to handle a seam failure — that
    would leak the implementation the seam exists to hide, and it is easy to get
    wrong in a way that only shows at runtime: `erpclaw_lib.db.db_error_types()`
    returns the raw DBAPI bases (`sqlite3.Error`, `psycopg2.Error`), and
    SQLAlchemy wraps every DBAPI failure in its own `SQLAlchemyError` hierarchy,
    so a DBAPI-only `except` silently catches nothing here. Found by
    `test_snapshot_read_error_returns_none` when module_manager's snapshot moved
    onto the seam and its "unreadable database ⇒ None" contract stopped holding.

    Includes the DBAPI bases too, for the paths that still hand back raw driver
    errors.
    """
    sa = _sqlalchemy()
    _missing, db_error_base = db_error_types()
    return (sa.exc.SQLAlchemyError, db_error_base)


def dispose_engines():
    """Drop every cached engine. For tests that switch database between cases."""
    with _engine_lock:
        for engine in _engines.values():
            engine.dispose()
        _engines.clear()


# ── Catalog introspection ────────────────────────────────────────────────────
#
# One answer to "what does this database contain", replacing the sqlite_master /
# information_schema hand-branches counted by the PG gate. PG-1 (ADR-0034 phase
# 4) is built on these.


def _inspector(db_path=None):
    sa = _sqlalchemy()
    return sa.inspect(get_engine(db_path))


def table_exists(name, db_path=None) -> bool:
    """Whether `name` exists, on any backend.

    Replaces ``SELECT name FROM sqlite_master WHERE type='table' AND name=?``,
    which is a hard error on PostgreSQL rather than a false.
    """
    return _inspector(db_path).has_table(name)


def table_names(db_path=None):
    """Every user table, sorted. Excludes each backend's own system catalog."""
    return sorted(_inspector(db_path).get_table_names())


def column_names(table, db_path=None):
    """Column names for `table`, in declaration order.

    Replaces ``PRAGMA table_info(x)``, which is not a statement PostgreSQL has.
    """
    return [c["name"] for c in _inspector(db_path).get_columns(table)]


def observer_catalog(conn):
    """Describe every object in the main schema through the caller handle.

    Reads the backend catalog with exactly the supplied connection: no
    environment lookup is performed and no second engine or connection is
    opened. One record is returned per object, in a deterministic order,
    each record carrying type, name, tbl_name and sql members. Sequence and
    automatic-index objects are included with no name-prefix filtering.
    Attached databases are not enumerated, and views are reported as views
    rather than re-read as tables.

    An unusable handle, an unreadable catalog, or an unknown backend raises
    instead of returning an empty list or any other success-shaped partial
    value.
    """
    if get_dialect() != "sqlite":
        raise RuntimeError(
            "observer_catalog supports the SQLite backend only."
        )
    if conn is None or not hasattr(conn, "execute"):
        raise TypeError(
            "observer_catalog requires a caller-supplied connection."
        )
    # Owner catalog read: every object in the main schema, ordered, through
    # the supplied read-only handle. The backend catalog statement lives here
    # in the seam, which owns dialect knowledge, like the other readers above.
    cursor = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "ORDER BY type, name, tbl_name"
    )
    rows = cursor.fetchall()
    records = [
        {"type": row[0], "name": row[1], "tbl_name": row[2], "sql": row[3]}
        for row in rows
    ]
    records.sort(key=lambda item: (
        "" if item["type"] is None else str(item["type"]),
        "" if item["name"] is None else str(item["name"]),
        "" if item["tbl_name"] is None else str(item["tbl_name"]),
    ))
    return records


def index_names(table, db_path=None):
    """Index names on `table`, sorted — including the ones SQLAlchemy skips.

    SQLAlchemy's reflection cannot describe an index built over an EXPRESSION
    (``lower(email)``). It drops those from ``get_indexes`` with a warning and
    returns the rest, so an index that exists in the database is reported as
    absent (ADR-0034 step 2f). A partial index — a plain column list with a
    ``WHERE`` clause — it does reflect; that was measured, not assumed.

    That is not cosmetic here. This function is what `describe_table` uses, and
    `describe_table` is phase 2's parity oracle: provision a module the old way
    and the new way, describe both, diff to zero. An index invisible to the
    oracle is an index a conversion may silently drop while the proof still says
    diff-to-zero. Measured across the 40 installers when this was found: 4 such
    indexes of 1,587, and all 4 are UNIQUE — `uq_crm_company_domain`,
    `uq_crm_contact_email`, `uq_crm_pipeline_name`, `uq_crm_pipeline_stage_name`
    in `erpclaw-growth`. They are uniqueness GUARANTEES, the most consequential
    kind of index to lose, and losing them would have proven correct.

    So the reflected names are unioned with the catalog's own list. The catalog
    query is dialect-specific and lives here, in the seam that owns dialect
    knowledge, rather than leaking into a module.
    """
    names = {i["name"] for i in _inspector(db_path).get_indexes(table)
             if i.get("name")}
    return sorted(names | _catalog_index_names(table, db_path))


def _catalog_index_names(table, db_path=None):
    """Index names straight from the backend catalog. Never raises.

    Best-effort by design: it exists to ADD what reflection missed, so a backend
    whose catalog we cannot read must degrade to reflection's answer rather than
    break introspection for everyone.
    """
    if get_dialect() == "postgresql":
        sql = "SELECT indexname FROM pg_indexes WHERE tablename = :t"
    else:
        sql = ("SELECT name FROM sqlite_master "
               "WHERE type = 'index' AND tbl_name = :t AND name IS NOT NULL")
    try:
        sa = _sqlalchemy()
        with get_engine(db_path).connect() as conn:
            rows = conn.execute(sa.text(sql), {"t": table}).fetchall()
    except Exception:
        return set()
    # SQLite names the implicit indexes behind UNIQUE constraints `sqlite_autoindex_*`.
    # They are not declared objects and no conversion can lose one on its own, so
    # counting them would make every comparison noisy for no signal.
    return {r[0] for r in rows if r[0] and not r[0].startswith("sqlite_auto")}


def _normalise_index_sql(sql):
    """Collapse the cosmetic differences between hand-written and emitted DDL.

    SQLAlchemy writes ``ON tbl (a, b)``; the hand-written installers write
    ``ON tbl(a, b)``. Nothing else about the statement may be normalised — the
    UNIQUE keyword, the expression and the WHERE clause are all load-bearing.
    """
    import re as _re

    return _re.sub(r"\s+\(", "(", " ".join((sql or "").split()))


def index_definitions(table, db_path=None):
    """``{index name: normalised CREATE INDEX text}`` for `table`.

    `index_names` answers "which indexes exist". That is not enough to certify a
    conversion, because an index can keep its name and lose its meaning:
    ADR-0034 bulk-39 measured three such defects on `erpclaw-growth` that a
    name-only comparison graded DIFF-TO-ZERO —

      * `lower(name)` dropped, leaving a case-sensitive index under the same name
      * the partial `WHERE email IS NOT NULL` dropped, changing which rows it covers
      * `UNIQUE` dropped, turning a uniqueness guarantee into a lookup hint

    Each of those silently weakens a constraint the product depends on, and each
    reads as identical if you only compare names. So the definition is the unit
    of comparison, taken from the catalog because SQLAlchemy refuses to reflect
    expression indexes at all.

    Never raises: it exists to strengthen a comparison, so a backend whose
    catalog we cannot read degrades to the weaker one rather than breaking it.
    """
    try:
        sa = _sqlalchemy()
        with get_engine(db_path).connect() as conn:
            if get_dialect() == "postgresql":
                rows = conn.execute(sa.text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE tablename = :t"), {"t": table}).fetchall()
            else:
                rows = conn.execute(sa.text(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type = 'index' AND tbl_name = :t"), {"t": table}).fetchall()
    except Exception:
        return {}
    return {r[0]: _normalise_index_sql(r[1])
            for r in rows
            if r[0] and r[1] and not r[0].startswith("sqlite_auto")}


def _catalog_unique_columns(table, db_path=None):
    """Unique-constraint column tuples, straight from the backend catalog.

    SQLAlchemy's SQLite reflection finds an inline ``UNIQUE`` by regex, and its
    pattern allows only ``[a-z0-9_ ]`` between the column name and the keyword
    (``dialects/sqlite/base.py`` INLINE_UNIQUE_PATTERN). So it matches
    ``naming_series TEXT NOT NULL UNIQUE DEFAULT ''`` — 21 sites in this tree —
    and MISSES ``naming_series TEXT NOT NULL DEFAULT '' UNIQUE``, which is the
    spelling `educlaw-scheduling` happens to use in 2 places.

    That asymmetry produced a false DIFFERS rather than a false green, which is
    the safer direction but still wrong: a converted installer declares
    ``unique=True``, SQLAlchemy emits a table-level ``UNIQUE (col)`` the same
    parser DOES see, and the comparison reported three constraints "added" that
    had been there all along. Both sides build identical indexes and both refuse
    duplicates; only the reading differed.

    The catalog does not have opinions about spelling. ``origin='u'`` is exactly
    "this index exists because of a UNIQUE constraint" (ADR-0034 bulk-39).

    Never raises: it exists to correct reflection, so an unreadable catalog
    degrades to reflection's answer.
    """
    try:
        sa = _sqlalchemy()
        with get_engine(db_path).connect() as conn:
            if get_dialect() == "postgresql":
                rows = conn.execute(sa.text("""
                    SELECT kcu.constraint_name, kcu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON kcu.constraint_name = tc.constraint_name
                     AND kcu.constraint_schema = tc.constraint_schema
                    WHERE tc.table_name = :t AND tc.constraint_type = 'UNIQUE'
                    ORDER BY kcu.constraint_name, kcu.ordinal_position
                """), {"t": table}).fetchall()
                grouped = {}
                for name, column in rows:
                    grouped.setdefault(name, []).append(column)
                return {tuple(cols) for cols in grouped.values()}

            found = set()
            for row in conn.execute(
                    sa.text(f'PRAGMA index_list("{table}")')).fetchall():
                # (seq, name, unique, origin, partial) — origin 'u' means the
                # index exists to enforce a UNIQUE constraint.
                if len(row) < 4 or row[3] != "u":
                    continue
                cols = conn.execute(
                    sa.text(f'PRAGMA index_info("{row[1]}")')).fetchall()
                found.add(tuple(c[2] for c in sorted(cols, key=lambda c: c[0])))
            return found
    except Exception:
        return set()


def _normalise_action(action):
    """`NO ACTION` and "unspecified" are the same referential action."""
    if not action or str(action).upper() in ("NO ACTION", "NONE"):
        return None
    return str(action).upper()


def _catalog_fk_ondelete(table, db_path=None):
    """`{constrained_columns: ON DELETE action}` from the backend catalog.

    SQLAlchemy reflects a foreign key's referential ACTION only when the DDL
    wrote it as a table-level ``FOREIGN KEY`` clause. ERPClaw's pre-conversion
    installers write the column-inline form —
    ``rule_id TEXT REFERENCES approval_rule(id) ON DELETE CASCADE`` — and for
    those, reflection returns the key with empty options while the database
    plainly has the action (ADR-0034 bulk-39).

    That asymmetry is worse than a plain blind spot. A converted installer
    declares its foreign keys as table-level clauses, so the SAME constraint
    reflects as `CASCADE` after conversion and as nothing before it, and a parity
    proof reading reflection alone reports a difference where there is none — and
    would have had a correct conversion "fixed" to match a misreading.

    Never raises: it exists to correct reflection, so a backend whose catalog we
    cannot read degrades to reflection's answer.
    """
    try:
        sa = _sqlalchemy()
        with get_engine(db_path).connect() as conn:
            if get_dialect() == "postgresql":
                rows = conn.execute(sa.text("""
                    SELECT kcu.column_name, rc.delete_rule
                    FROM information_schema.referential_constraints rc
                    JOIN information_schema.key_column_usage kcu
                      ON kcu.constraint_name = rc.constraint_name
                     AND kcu.constraint_schema = rc.constraint_schema
                    WHERE kcu.table_name = :t
                    ORDER BY kcu.ordinal_position
                """), {"t": table}).fetchall()
                out = {}
                for column, action in rows:
                    out[(column,)] = action
                return out
            rows = conn.execute(
                sa.text(f'PRAGMA foreign_key_list("{table}")')).fetchall()
    except Exception:
        return {}
    # PRAGMA columns: id, seq, table, from, to, on_update, on_delete, match.
    # `id` groups the columns of a composite key; `seq` orders them.
    grouped = {}
    for row in rows:
        grouped.setdefault(row[0], []).append(row)
    out = {}
    for parts in grouped.values():
        parts.sort(key=lambda r: r[1])
        out[tuple(p[3] for p in parts)] = parts[0][6]
    return out


def _normalise_partial_predicate(raw):
    """Dialect-neutral text of one partial-index predicate.

    The SQLite reflection hands back a bound-text object while the PostgreSQL
    reflection hands back plain text, and the latter may spell a comparison
    with an explicit type suffix; neither difference changes which rows the
    index covers, so both are folded away here. One enclosing pair of
    parentheses is stripped while the whole predicate is wrapped in exactly
    one, because each backend parenthesises the stored form its own way.
    """
    import re as _re

    text = " ".join(str(raw).split())
    text = _re.sub(r"::[A-Za-z_][A-Za-z0-9_ ]*", "", text)
    text = " ".join(text.split())
    while len(text) >= 2 and text.startswith("(") and text.endswith(")"):
        depth = 0
        wrapped = True
        for pos, char in enumerate(text):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            if depth == 0 and pos < len(text) - 1:
                wrapped = False
                break
        if not wrapped:
            break
        text = " ".join(text[1:-1].split())
    return text


def _partial_unique_entries(table, db_path=None):
    """`(name, columns, predicate)` for each live partial unique index.

    Columns come from the inspector's own index listing; the predicate comes
    from the same listing's backend-specific qualifier, or — where the
    vendored reflection leaves that qualifier empty — from the text after the
    qualifier keyword in this table's already-reported index definition, so
    no second trip to the backend catalog is needed here.
    """
    insp = _inspector(db_path)
    if get_dialect() == "postgresql":
        first_key, second_key = "postgresql_where", "sqlite_where"
    else:
        first_key, second_key = "sqlite_where", "postgresql_where"
    defs = index_definitions(table, db_path)
    out = []
    for entry in insp.get_indexes(table):
        if not entry.get("unique"):
            continue
        options = entry.get("dialect_options") or {}
        raw = options.get(first_key, options.get(second_key))
        if raw is None or str(raw).strip() == "":
            definition = defs.get(entry.get("name") or "")
            parts = (definition or "").split(" WHERE ", 1)
            if len(parts) != 2:
                continue
            raw = parts[1]
        out.append((entry.get("name"), tuple(entry.get("column_names") or []),
                    _normalise_partial_predicate(raw)))
    return sorted(out)


def describe_constraints(table, db_path=None):
    """CHECK bodies, foreign keys and column defaults — what `describe_table` omits.

    `describe_table` answers "is the shape the same". It cannot answer "does this
    column still refuse a bad value", because it carries no CHECK bodies, no
    foreign-key targets and no defaults. For ADR-0034 phase 2 that gap is the
    difference between proving a conversion structurally identical and proving it
    behaviourally identical: a converted installer that dropped
    ``CHECK(status IN (...))`` or pointed a foreign key at the wrong table would
    diff to zero on shape alone.

    CHECK constraints are compared by BODY, never by name. The pre-conversion DDL
    writes them inline and unnamed; SQLAlchemy requires a name to emit one. So the
    names legitimately differ between the two sides and only the predicate is
    evidence.
    """
    insp = _inspector(db_path)
    checks = sorted(
        " ".join((c.get("sqltext") or "").split())
        for c in insp.get_check_constraints(table))
    ondelete = _catalog_fk_ondelete(table, db_path)
    foreign_keys = sorted(
        (
            tuple(f.get("constrained_columns") or []),
            f.get("referred_table"),
            tuple(f.get("referred_columns") or []),
            _normalise_action(
                ondelete.get(tuple(f.get("constrained_columns") or []))
                or (f.get("options") or {}).get("ondelete")),
        )
        for f in insp.get_foreign_keys(table))
    defaults = sorted(
        (c["name"], None if c.get("default") is None else str(c["default"]))
        for c in insp.get_columns(table))
    # Unique constraints are reported by their COLUMNS, not their names. A
    # table-level `UNIQUE (a, b)` written inline is unnamed, and SQLite implements
    # it with an implicit `sqlite_autoindex_*` that `index_names` deliberately
    # filters out — so a conversion that dropped one left no trace anywhere in
    # this description and would have diffed to zero (ADR-0034 bulk-39, found on
    # erpclaw-integrations' `integration_entity_map`). The columns are the
    # constraint's identity; the name is incidental and differs between a
    # hand-written UNIQUE and a declared UniqueConstraint.
    uniques = sorted(
        {tuple(u.get("column_names") or [])
         for u in insp.get_unique_constraints(table)}
        | _catalog_unique_columns(table, db_path))
    return {"checks": checks, "foreign_keys": foreign_keys,
            "defaults": defaults, "uniques": uniques,
            "index_defs": index_definitions(table, db_path),
            "partial_unique": _partial_unique_entries(table, db_path)}


def describe_table(table, db_path=None):
    """A structural description of `table`, for comparing two provisioning routes.

    Includes the reflected column TYPE. An earlier version left types out on the
    reasoning that backends spell the same declared type differently — true, but
    irrelevant here and actively dangerous: this description exists for ADR-0034
    phase 2's parity proof, which provisions a module the old way and the new way
    **on the same backend** and diffs. Same backend means type strings are
    directly comparable, and a type change is the single most consequential thing
    a schema conversion can get wrong. Without types, converting every ID column
    from TEXT to VARCHAR(36) across 792 tables would diff to zero and read as
    proof of correctness.

    Money columns are TEXT on every backend (ADR-0034 dec. 1); this is the check
    that would notice if a conversion quietly changed that.
    """
    insp = _inspector(db_path)
    cols = insp.get_columns(table)
    pk = insp.get_pk_constraint(table) or {}
    return {
        "columns": [
            {
                "name": c["name"],
                "type": str(c["type"]).upper(),
                "nullable": bool(c["nullable"]),
            }
            for c in cols
        ],
        "primary_key": sorted(pk.get("constrained_columns") or []),
        "indexes": index_names(table, db_path),
    }


def declared_type(column, db_path=None) -> str:
    """Declared column type as spelled by the active backend dialect."""
    return column.type.compile(dialect=get_engine(db_path).dialect).upper()


# ── Authority-core schema evidence (m242, internal transport) ────────────────
#
# A minimal read-only capture of the stored schema text for the fixed
# eight-table authority-core profile. This helper is INTERNAL and unwired: no
# production caller uses it. It is not a validator and returns no admission
# or validity flag — only the raw stored rows for the profile tables in the
# main and temp namespaces of the SUPPLIED connection.
#
# Callers keep their own transaction open across the call; cursors opened
# here are the only objects this helper touches or closes.
#
# One oversized native cell can allocate before Python measures it: the byte
# budget below bounds what is KEPT, not what the driver transiently holds,
# so it is not a hard native allocation cap.

_AUTHORITY_CORE_TABLES = (
    "authority_install",
    "authority_principal",
    "authority_membership",
    "authority_right",
    "authority_delegation",
    "authority_delegation_right",
    "authority_delegation_cap",
    "operation_authorization",
)


def authority_core_metadata():
    """The shipped declaration of the frozen authority-core profile; the L0 fixture pins its shape."""
    sa = _sqlalchemy()
    meta = sa.MetaData()
    sa.Table(
        "authority_install", meta,
        sa.Column("singleton", sa.Integer,
                  nullable=False, primary_key=True),
        sa.Column("install_id", sa.Text, nullable=False, unique=True),
        sa.Column("schema_version", sa.Integer, nullable=False),
        sa.Column("phase", sa.Text, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.CheckConstraint("singleton = 1"),
        sa.CheckConstraint("schema_version = 1"),
        sa.CheckConstraint("phase IN ('STAGED', 'ACTIVE')"),
        sa.CheckConstraint("revision >= 0"),
    )
    sa.Table(
        "authority_principal", meta,
        sa.Column("install_id", sa.Text, nullable=False),
        sa.Column("id", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("disabled_at", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.PrimaryKeyConstraint("install_id", "id"),
        sa.ForeignKeyConstraint(
            ["install_id"], ["authority_install.install_id"],
            onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.CheckConstraint("kind IN ('human', 'service')"),
        sa.CheckConstraint("(disabled_at IS NULL OR disabled_at >= 0)"),
    )
    sa.Table(
        "authority_membership", meta,
        sa.Column("install_id", sa.Text, nullable=False),
        sa.Column("principal_id", sa.Text, nullable=False),
        sa.Column("company_id", sa.Text, nullable=False),
        sa.Column("effect", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint(
            "install_id", "principal_id", "company_id", "effect"),
        sa.ForeignKeyConstraint(
            ["install_id", "principal_id"],
            ["authority_principal.install_id", "authority_principal.id"],
            onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.CheckConstraint("effect IN ('allow', 'deny')"),
    )
    sa.Table(
        "authority_right", meta,
        sa.Column("install_id", sa.Text, nullable=False),
        sa.Column("principal_id", sa.Text, nullable=False),
        sa.Column("company_id", sa.Text, nullable=False),
        sa.Column("resource_kind", sa.Text, nullable=False),
        sa.Column("resource_id", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("effect", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint(
            "install_id", "principal_id", "company_id", "resource_kind",
            "resource_id", "action", "effect"),
        sa.ForeignKeyConstraint(
            ["install_id", "principal_id"],
            ["authority_principal.install_id", "authority_principal.id"],
            onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.CheckConstraint("effect IN ('allow', 'deny')"),
    )
    sa.Table(
        "authority_delegation", meta,
        sa.Column("install_id", sa.Text, nullable=False),
        sa.Column("id", sa.Text, nullable=False),
        sa.Column("issuer_id", sa.Text, nullable=False),
        sa.Column("grantee_id", sa.Text, nullable=False),
        sa.Column("issued_at", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("expires_at", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("revoked_at", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.PrimaryKeyConstraint("install_id", "id"),
        sa.ForeignKeyConstraint(
            ["install_id", "issuer_id"],
            ["authority_principal.install_id", "authority_principal.id"],
            onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["install_id", "grantee_id"],
            ["authority_principal.install_id", "authority_principal.id"],
            onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.CheckConstraint("(issued_at >= 0 AND issued_at < expires_at)"),
        sa.CheckConstraint("(revoked_at IS NULL OR revoked_at >= 0)"),
        sa.CheckConstraint("issuer_id <> grantee_id"),
    )
    sa.Table(
        "authority_delegation_right", meta,
        sa.Column("install_id", sa.Text, nullable=False),
        sa.Column("delegation_id", sa.Text, nullable=False),
        sa.Column("company_id", sa.Text, nullable=False),
        sa.Column("resource_kind", sa.Text, nullable=False),
        sa.Column("resource_id", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint(
            "install_id", "delegation_id", "company_id", "resource_kind",
            "resource_id", "action"),
        sa.ForeignKeyConstraint(
            ["install_id", "delegation_id"],
            ["authority_delegation.install_id", "authority_delegation.id"],
            onupdate="RESTRICT", ondelete="RESTRICT"),
    )
    sa.Table(
        "authority_delegation_cap", meta,
        sa.Column("install_id", sa.Text, nullable=False),
        sa.Column("delegation_id", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("currency", sa.Text, nullable=False),
        sa.Column("scale", sa.Integer, nullable=False),
        sa.Column("per_operation", sa.Text, nullable=False),
        sa.Column("aggregate_limit", sa.Text, nullable=False),
        sa.Column("window_start", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("window_end", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.PrimaryKeyConstraint(
            "install_id", "delegation_id", "action", "currency",
            "window_start", "window_end"),
        sa.ForeignKeyConstraint(
            ["install_id", "delegation_id"],
            ["authority_delegation.install_id", "authority_delegation.id"],
            onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.CheckConstraint("(scale >= 0 AND scale <= 18)"),
        sa.CheckConstraint(
            "(window_start >= 0 AND window_start < window_end)"),
    )
    sa.Table(
        "operation_authorization", meta,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("install_id", sa.Text, nullable=False),
        sa.Column("principal_id", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("binding_digest", sa.Text, nullable=False),
        sa.Column("delegation_id", sa.Text, nullable=True),
        sa.Column("issued_at", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("expires_at", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("revoked_at", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("consumed_at", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("consumed_txn", sa.Text, nullable=True),
        sa.CheckConstraint("expires_at >= issued_at AND issued_at >= 0"),
        sa.CheckConstraint(
            "((consumed_at IS NULL AND consumed_txn IS NULL) OR "
            "(consumed_at IS NOT NULL AND consumed_txn IS NOT NULL))"),
        sa.ForeignKeyConstraint(
            ["install_id", "principal_id"],
            ["authority_principal.install_id", "authority_principal.id"],
            onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["install_id", "delegation_id"],
            ["authority_delegation.install_id", "authority_delegation.id"],
            onupdate="RESTRICT", ondelete="RESTRICT"),
    )
    return meta


def provision_authority_core(db_path=None, seed=True) -> dict:
    """Ensure the eight authority-core tables exist with one install record.

    Tables are added only when absent and the install record is written only
    when authority_install is empty; a record already present is never changed.
    """
    authority_core_metadata().create_all(get_engine(db_path), checkfirst=True)
    # The checkfirst emit costs about 0.09 s here against about 0.65 s for
    # the full snapshot route on a full foundation database, so the snapshot
    # route is not used.
    if not seed:
        return {"install_seeded": False}
    import uuid as _uuid
    from erpclaw_lib import db as _db
    from erpclaw_lib.query import P as _P
    from erpclaw_lib.query import Q as _Q
    from erpclaw_lib.query import Table as _T
    _install = _T("authority_install")
    _conn = _db.get_connection(db_path)
    try:
        _row = _conn.execute(
            _Q.from_(_install).select(_install.singleton).get_sql()
        ).fetchone()
        if _row is not None:
            return {"install_seeded": False}
        _new_id = str(_uuid.uuid4())
        _insert = _Q.into(_install).columns(
            "singleton", "install_id", "schema_version", "phase",
            "revision").insert(_P(), _P(), _P(), _P(), _P())
        try:
            _conn.execute(_insert.get_sql(), (1, _new_id, 1, "STAGED", 0))
            _conn.commit()
        except _db.integrity_error_types():
            _conn.rollback()
            _again = _conn.execute(
                _Q.from_(_install).select(_install.singleton).get_sql()
            ).fetchone()
            if _again is None:
                raise
            return {"install_seeded": False}
        return {"install_seeded": True}
    finally:
        _conn.close()


_AUTHORITY_ENVELOPE_TABLES = (
    "operation_authorization_envelope",
    "operation_authorization_result",
    "authority_delegation_usage",
)


def authority_envelope_metadata():
    """The shipped declaration of the three authorization-envelope tables."""
    sa = _sqlalchemy()
    meta = authority_core_metadata()
    sa.Table(
        "operation_authorization_envelope", meta,
        sa.Column("authorization_id", sa.Text, primary_key=True),
        sa.Column("install_id", sa.Text, nullable=False),
        sa.Column("principal_id", sa.Text, nullable=False),
        sa.Column("envelope_version", sa.Integer, nullable=False),
        sa.Column("args_digest", sa.Text, nullable=False),
        sa.Column("issuer_id", sa.Text, nullable=False),
        sa.Column("issued_route", sa.Text, nullable=False),
        sa.Column("reason_code", sa.Text, nullable=False),
        sa.Column("reason_text", sa.Text, nullable=False),
        sa.Column("idempotency_key", sa.Text, nullable=False),
        sa.Column("call_id", sa.Text, nullable=True),
        sa.Column("envelope_digest", sa.Text, nullable=False),
        sa.ForeignKeyConstraint(
            ["authorization_id"], ["operation_authorization.id"],
            onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["install_id", "principal_id"],
            ["authority_principal.install_id", "authority_principal.id"],
            onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["install_id", "issuer_id"],
            ["authority_principal.install_id", "authority_principal.id"],
            onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "install_id", "principal_id", "idempotency_key"),
        sa.CheckConstraint("envelope_version = 2"),
        sa.CheckConstraint(
            "issued_route IN "
            "('delegation', 'exact_approval', 'staged_unattested')"),
        sa.CheckConstraint("length(args_digest) = 64"),
        sa.CheckConstraint("length(envelope_digest) = 64"),
        sa.CheckConstraint("length(reason_text) <= 280"),
    )
    sa.Table(
        "operation_authorization_result", meta,
        sa.Column("authorization_id", sa.Text, primary_key=True),
        sa.Column("consumed_txn", sa.Text, nullable=False),
        sa.Column("result_kind", sa.Text, nullable=False),
        sa.Column("result_status", sa.Text, nullable=False),
        sa.Column("result_id", sa.Text, nullable=True),
        sa.Column("recorded_at", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.ForeignKeyConstraint(
            ["authorization_id"], ["operation_authorization.id"],
            onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.CheckConstraint("recorded_at >= 0"),
    )
    sa.Table(
        "authority_delegation_usage", meta,
        sa.Column("install_id", sa.Text, nullable=False),
        sa.Column("delegation_id", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("currency", sa.Text, nullable=False),
        sa.Column("window_start", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("window_end", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("used", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint(
            "install_id", "delegation_id", "action", "currency",
            "window_start", "window_end"),
        sa.ForeignKeyConstraint(
            ["install_id", "delegation_id", "action", "currency",
             "window_start", "window_end"],
            ["authority_delegation_cap.install_id",
             "authority_delegation_cap.delegation_id",
             "authority_delegation_cap.action",
             "authority_delegation_cap.currency",
             "authority_delegation_cap.window_start",
             "authority_delegation_cap.window_end"],
            onupdate="RESTRICT", ondelete="RESTRICT"),
    )
    return meta


def provision_authority_envelope(db_path=None) -> dict:
    """Ensure the three authorization-envelope tables exist, empty.

    Tables are added only when absent; rows already stored are never
    changed. The authority core must already be present.
    """
    have = set(table_names(db_path))
    if any(name not in have for name in _AUTHORITY_CORE_TABLES):
        raise RuntimeError(
            "authorization envelope needs the authority core: CORE_ABSENT")
    absent = [name for name in _AUTHORITY_ENVELOPE_TABLES if name not in have]
    meta = authority_envelope_metadata()
    meta.create_all(
        get_engine(db_path),
        tables=[meta.tables[name] for name in _AUTHORITY_ENVELOPE_TABLES],
        checkfirst=True)
    return {"created": absent}


_AUTHORITY_SESSION_TABLES = (
    "authority_deployment",
    "authority_credential",
    "authority_session",
    "authority_bootstrap_challenge",
    "operation_authorization_issuer",
)


def authority_session_metadata():
    """The shipped declaration of the five session/credential tables."""
    sa = _sqlalchemy()
    meta = authority_envelope_metadata()
    sa.Table(
        "authority_deployment", meta,
        sa.Column("install_id", sa.Text, primary_key=True),
        sa.Column("operator_account", sa.Text, nullable=False),
        sa.Column("service_account", sa.Text, nullable=False),
        sa.Column("model_accounts", sa.Text, nullable=False),
        sa.Column("expected_install_id", sa.Text, nullable=False),
        sa.Column("recorded_at",
                  sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  nullable=False),
        sa.ForeignKeyConstraint(
            ["install_id"], ["authority_install.install_id"],
            onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.CheckConstraint("operator_account <> service_account"),
        sa.CheckConstraint("recorded_at >= 0"),
        sa.CheckConstraint("length(operator_account) BETWEEN 1 AND 64"),
        sa.CheckConstraint("length(service_account) BETWEEN 1 AND 64"),
        sa.CheckConstraint("length(expected_install_id) BETWEEN 1 AND 128"),
    )
    sa.Table(
        "authority_credential", meta,
        sa.Column("install_id", sa.Text, nullable=False),
        sa.Column("id", sa.Text, nullable=False),
        sa.Column("principal_id", sa.Text, nullable=False),
        sa.Column("scheme", sa.Text, nullable=False),
        sa.Column("verifier", sa.Text, nullable=False),
        sa.Column("created_at",
                  sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  nullable=False),
        sa.Column("retired_at",
                  sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  nullable=True),
        sa.PrimaryKeyConstraint("install_id", "id"),
        sa.ForeignKeyConstraint(
            ["install_id", "principal_id"],
            ["authority_principal.install_id", "authority_principal.id"],
            onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.CheckConstraint("scheme = 'pbkdf2-sha256'"),
        sa.CheckConstraint(
            "substr(verifier, 1, 14) = 'pbkdf2:600000$'"),
        sa.CheckConstraint("length(verifier) = 111"),
        sa.CheckConstraint(
            "retired_at IS NULL OR retired_at >= created_at"),
        sa.Index("ux_authority_credential_live", "install_id",
                 "principal_id", unique=True,
                 sqlite_where=sa.text("retired_at IS NULL"),
                 postgresql_where=sa.text("retired_at IS NULL")),
    )
    sa.Table(
        "authority_session", meta,
        sa.Column("install_id", sa.Text, nullable=False),
        sa.Column("id_digest", sa.Text, nullable=False),
        sa.Column("principal_id", sa.Text, nullable=False),
        sa.Column("created_at",
                  sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  nullable=False),
        sa.Column("expires_at",
                  sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  nullable=False),
        sa.Column("revoked_at",
                  sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  nullable=True),
        sa.Column("created_account", sa.Text, nullable=False),
        sa.Column("route", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("install_id", "id_digest"),
        sa.ForeignKeyConstraint(
            ["install_id", "principal_id"],
            ["authority_principal.install_id", "authority_principal.id"],
            onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.CheckConstraint("length(id_digest) = 64"),
        sa.CheckConstraint("created_at < expires_at"),
        sa.CheckConstraint("expires_at - created_at <= 28800000"),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at"),
        sa.CheckConstraint("route IN ('operator-tty')"),
    )
    sa.Table(
        "authority_bootstrap_challenge", meta,
        sa.Column("install_id", sa.Text, nullable=False),
        sa.Column("id", sa.Text, nullable=False),
        sa.Column("digest", sa.Text, nullable=False),
        sa.Column("issued_at",
                  sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  nullable=False),
        sa.Column("expires_at",
                  sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  nullable=False),
        sa.Column("consumed_at",
                  sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  nullable=True),
        sa.Column("rotated_at",
                  sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  nullable=True),
        sa.Column("consumed_by", sa.Text, nullable=True),
        sa.Column("state", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("install_id", "id"),
        sa.ForeignKeyConstraint(
            ["install_id"], ["authority_install.install_id"],
            onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.CheckConstraint("length(digest) = 64"),
        sa.CheckConstraint("state IN ('issued', 'consumed', 'rotated')"),
        sa.CheckConstraint(
            "(state = 'issued' AND consumed_at IS NULL AND "
            "rotated_at IS NULL AND consumed_by IS NULL) OR "
            "(state = 'consumed' AND consumed_at IS NOT NULL AND "
            "consumed_by IS NOT NULL AND rotated_at IS NULL) OR "
            "(state = 'rotated' AND rotated_at IS NOT NULL AND "
            "consumed_at IS NULL AND consumed_by IS NULL)"),
        sa.CheckConstraint(
            "issued_at < expires_at AND expires_at - issued_at <= 3600000"),
        sa.Index("ux_authority_bootstrap_issued", "install_id", unique=True,
                 sqlite_where=sa.text("state = 'issued'"),
                 postgresql_where=sa.text("state = 'issued'")),
    )
    sa.Table(
        "operation_authorization_issuer", meta,
        sa.Column("authorization_id", sa.Text, primary_key=True),
        sa.Column("install_id", sa.Text, nullable=False),
        sa.Column("issuer_id", sa.Text, nullable=False),
        sa.Column("session_digest", sa.Text, nullable=False),
        sa.ForeignKeyConstraint(
            ["authorization_id"], ["operation_authorization.id"],
            onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["install_id", "issuer_id"],
            ["authority_principal.install_id", "authority_principal.id"],
            onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.CheckConstraint("length(session_digest) = 64"),
    )
    return meta


def provision_authority_sessions(db_path=None) -> dict:
    """Ensure the five session/credential tables exist, empty.

    Tables are added only when absent; rows already stored are never
    changed. The authority core and the authorization envelope must already
    be present.
    """
    have = set(table_names(db_path))
    if any(name not in have
           for name in _AUTHORITY_CORE_TABLES + _AUTHORITY_ENVELOPE_TABLES):
        raise RuntimeError(
            "authority sessions need the authorization envelope: "
            "ENVELOPE_ABSENT")
    absent = [name for name in _AUTHORITY_SESSION_TABLES if name not in have]
    meta = authority_session_metadata()
    meta.create_all(
        get_engine(db_path),
        tables=[meta.tables[name] for name in _AUTHORITY_SESSION_TABLES],
        checkfirst=True)
    return {"created": absent}


_AUTHORITY_CORE_CATALOG_SQL = (
    "SELECT type, name, tbl_name, sql FROM \"%s\".sqlite_master "
    "WHERE lower(name) IN (?, ?, ?, ?, ?, ?, ?, ?) "
    "OR lower(tbl_name) IN (?, ?, ?, ?, ?, ?, ?, ?)"
)

_AUTHORITY_CORE_ROW_LIMIT = 128
_AUTHORITY_CORE_BYTE_LIMIT = 1048576


def authority_core_schema_evidence(conn):
    """Read-only capture of stored core-profile schema text.

    Returns an immutable tuple of immutable
    ``(namespace, type, name, tbl_name, sql)`` tuples for the eight fixed
    profile tables, read from the SUPPLIED connection in the ``main`` and
    ``temp`` namespaces and deterministically sorted (a ``None`` sql sorts
    as an empty string). This is raw evidence, not a validity finding: it
    reports what this connection stores and says nothing about admission,
    validity or readiness.

    Only an exact ``db.ConnectionWrapper`` holding an exact native
    connection is accepted, with the caller-owned write transaction already
    active and the native text decoder left at ``str``. Anything else fails
    with a fixed token and no detail. At most 128 matching rows per
    namespace and at most 1 MiB of kept text across both captures; beyond
    either the call fails rather than returning a prefix. Only cursors
    opened here are touched, and only to neutralise their row shape; the
    connection itself is never configured, advanced, closed, or handed to
    another opener.
    """
    import sqlite3 as _sqlite3

    from erpclaw_lib.db import ConnectionWrapper as _ConnectionWrapper

    if type(conn) is not _ConnectionWrapper:
        raise ValueError("AUTHORITY_CORE_UNSUPPORTED") from None
    try:
        raw = object.__getattribute__(conn, "_conn")
    except Exception:
        raise ValueError("AUTHORITY_CORE_UNSUPPORTED") from None
    if type(raw) is not _sqlite3.Connection:
        raise ValueError("AUTHORITY_CORE_UNSUPPORTED") from None
    try:
        text_factory = raw.text_factory
        in_transaction = raw.in_transaction
        isolation_level = raw.isolation_level
    except Exception:
        raise RuntimeError("AUTHORITY_CORE_STORAGE_ERROR") from None
    if text_factory is not str:
        raise ValueError("AUTHORITY_CORE_UNSUPPORTED") from None
    if in_transaction is not True or isolation_level is None:
        raise ValueError("AUTHORITY_CORE_TRANSACTION_REQUIRED") from None
    params = list(_AUTHORITY_CORE_TABLES) + list(_AUTHORITY_CORE_TABLES)
    rows = []
    kept = 0
    opened = []
    failed_close = False
    try:
        for namespace in ("main", "temp"):
            try:
                cursor = raw.cursor()
            except Exception:
                raise RuntimeError("AUTHORITY_CORE_STORAGE_ERROR") from None
            opened.append(cursor)
            try:
                cursor.row_factory = None
                cursor.execute(_AUTHORITY_CORE_CATALOG_SQL % namespace, params)
            except Exception:
                raise RuntimeError("AUTHORITY_CORE_STORAGE_ERROR") from None
            seen = 0
            while True:
                try:
                    record = cursor.fetchone()
                except Exception:
                    raise RuntimeError("AUTHORITY_CORE_STORAGE_ERROR") from None
                if record is None:
                    break
                seen += 1
                if seen > _AUTHORITY_CORE_ROW_LIMIT:
                    raise RuntimeError("AUTHORITY_CORE_INCOMPLETE") from None
                if type(record) is not tuple or len(record) != 4:
                    raise RuntimeError(
                        "AUTHORITY_CORE_STORAGE_INVALID") from None
                kind, name, tbl_name, sql = record
                if type(kind) is not str or type(name) is not str \
                        or type(tbl_name) is not str:
                    raise RuntimeError(
                        "AUTHORITY_CORE_STORAGE_INVALID") from None
                if sql is not None and type(sql) is not str:
                    raise RuntimeError(
                        "AUTHORITY_CORE_STORAGE_INVALID") from None
                kept += len(namespace.encode("utf-8")) \
                    + len(kind.encode("utf-8")) \
                    + len(name.encode("utf-8")) \
                    + len(tbl_name.encode("utf-8")) \
                    + (0 if sql is None else len(sql.encode("utf-8")))
                if kept > _AUTHORITY_CORE_BYTE_LIMIT:
                    raise RuntimeError("AUTHORITY_CORE_INCOMPLETE") from None
                rows.append((namespace, kind, name, tbl_name, sql))
    finally:
        for cursor in opened:
            try:
                cursor.close()
            except Exception:
                failed_close = True
    if failed_close:
        raise RuntimeError("AUTHORITY_CORE_STORAGE_ERROR") from None
    rows.sort(key=lambda item: (
        item[0], item[1], item[2], item[3],
        item[4] if item[4] is not None else ""))
    return tuple(rows)
# ── Authority-core same-connection observation (m242) ────────────────────────
#
# A single internal observer for the frozen eight-table core profile. It reads
# only the connection it is handed, inside the caller-owned open transaction,
# and returns a read-only four-key mapping. A MATCH is an observation taken at
# one instant: it grants nothing, authenticates nothing, chooses no cap and
# binds no later write. There is no wiring to any caller.
#
# Fixed read-only statement templates below, one per inspection operation.
# Table, column and index spellings come only from the closed tuples beside
# them; no identifier is caller-supplied and no statement is built from row
# data. The six native-catalog templates are the only new pattern sites in
# this module; the retained evidence template above is untouched and is
# composed with, never duplicated.
#
# Frozen expectation data (automatic-index names, stored-schema rows, column /
# index / dependency observations) was derived once during authoring from the
# qualified frozen artifact plus the exact native metadata of that fixture,
# pinned to native SQLite 3.45.1 and the vendored SQLAlchemy 2.0.51
# representation. Any toolchain or representation change refuses as a
# structure mismatch until separately reviewed.
#
# Stored main-namespace rows are bound by one digest derived from the frozen
# artifact and verified by the test module.

_AUTHORITY_CORE_PROFILE = "authority-core-v1-sqlite"
_AUTHORITY_CORE_SQLITE_VERSION = "3.45.1"
_AUTHORITY_CORE_SQLALCHEMY_VERSION = "2.0.51"
_AUTHORITY_CORE_INT_MAX = 9223372036854775807
_AUTHORITY_CORE_INSPECT_ROW_LIMIT = 10000
_AUTHORITY_CORE_INSPECT_TEXT_LIMIT = 1048576
_AUTHORITY_CORE_FETCH_BATCH = 500

_AUTHORITY_CORE_FK_STATE_SQL = "PRAGMA foreign_keys"
_AUTHORITY_CORE_DB_LIST_SQL = "PRAGMA database_list"
_AUTHORITY_CORE_TABLE_SHAPE_SQL = "PRAGMA table_xinfo(\"%s\")"
_AUTHORITY_CORE_INDEX_LIST_SQL = "PRAGMA index_list(\"%s\")"
_AUTHORITY_CORE_INDEX_DETAIL_SQL = "PRAGMA index_xinfo(\"%s\")"
_AUTHORITY_CORE_FOREIGN_KEY_SQL = "PRAGMA foreign_key_list(\"%s\")"

_AUTHORITY_CORE_AUTO_INDEXES = (
    'sqlite_autoindex_authority_delegation_1',
    'sqlite_autoindex_authority_delegation_cap_1',
    'sqlite_autoindex_authority_delegation_right_1',
    'sqlite_autoindex_authority_install_1',
    'sqlite_autoindex_authority_membership_1',
    'sqlite_autoindex_authority_principal_1',
    'sqlite_autoindex_authority_right_1',
    'sqlite_autoindex_operation_authorization_1',
)

_AUTHORITY_CORE_RAW_MAIN_DIGEST = "9b8e0b50fabe01de6412e704ad0305f6b024be9d099cdaee061cc92a1869b3c2"
_AUTHORITY_CORE_COLUMNS = (
    (  # authority_install
        (0, 'singleton', 'INTEGER', 1, None, 1, 0),
        (1, 'install_id', 'TEXT', 1, None, 0, 0),
        (2, 'schema_version', 'INTEGER', 1, None, 0, 0),
        (3, 'phase', 'TEXT', 1, None, 0, 0),
        (4, 'revision', 'INTEGER', 1, None, 0, 0),
    ),
    (  # authority_principal
        (0, 'install_id', 'TEXT', 1, None, 1, 0),
        (1, 'id', 'TEXT', 1, None, 2, 0),
        (2, 'kind', 'TEXT', 1, None, 0, 0),
        (3, 'disabled_at', 'INTEGER', 0, None, 0, 0),
    ),
    (  # authority_membership
        (0, 'install_id', 'TEXT', 1, None, 1, 0),
        (1, 'principal_id', 'TEXT', 1, None, 2, 0),
        (2, 'company_id', 'TEXT', 1, None, 3, 0),
        (3, 'effect', 'TEXT', 1, None, 4, 0),
    ),
    (  # authority_right
        (0, 'install_id', 'TEXT', 1, None, 1, 0),
        (1, 'principal_id', 'TEXT', 1, None, 2, 0),
        (2, 'company_id', 'TEXT', 1, None, 3, 0),
        (3, 'resource_kind', 'TEXT', 1, None, 4, 0),
        (4, 'resource_id', 'TEXT', 1, None, 5, 0),
        (5, 'action', 'TEXT', 1, None, 6, 0),
        (6, 'effect', 'TEXT', 1, None, 7, 0),
    ),
    (  # authority_delegation
        (0, 'install_id', 'TEXT', 1, None, 1, 0),
        (1, 'id', 'TEXT', 1, None, 2, 0),
        (2, 'issuer_id', 'TEXT', 1, None, 0, 0),
        (3, 'grantee_id', 'TEXT', 1, None, 0, 0),
        (4, 'issued_at', 'INTEGER', 1, None, 0, 0),
        (5, 'expires_at', 'INTEGER', 1, None, 0, 0),
        (6, 'revoked_at', 'INTEGER', 0, None, 0, 0),
    ),
    (  # authority_delegation_right
        (0, 'install_id', 'TEXT', 1, None, 1, 0),
        (1, 'delegation_id', 'TEXT', 1, None, 2, 0),
        (2, 'company_id', 'TEXT', 1, None, 3, 0),
        (3, 'resource_kind', 'TEXT', 1, None, 4, 0),
        (4, 'resource_id', 'TEXT', 1, None, 5, 0),
        (5, 'action', 'TEXT', 1, None, 6, 0),
    ),
    (  # authority_delegation_cap
        (0, 'install_id', 'TEXT', 1, None, 1, 0),
        (1, 'delegation_id', 'TEXT', 1, None, 2, 0),
        (2, 'action', 'TEXT', 1, None, 3, 0),
        (3, 'currency', 'TEXT', 1, None, 4, 0),
        (4, 'scale', 'INTEGER', 1, None, 0, 0),
        (5, 'per_operation', 'TEXT', 1, None, 0, 0),
        (6, 'aggregate_limit', 'TEXT', 1, None, 0, 0),
        (7, 'window_start', 'INTEGER', 1, None, 5, 0),
        (8, 'window_end', 'INTEGER', 1, None, 6, 0),
    ),
    (  # operation_authorization
        (0, 'id', 'TEXT', 1, None, 1, 0),
        (1, 'install_id', 'TEXT', 1, None, 0, 0),
        (2, 'principal_id', 'TEXT', 1, None, 0, 0),
        (3, 'action', 'TEXT', 1, None, 0, 0),
        (4, 'binding_digest', 'TEXT', 1, None, 0, 0),
        (5, 'delegation_id', 'TEXT', 0, None, 0, 0),
        (6, 'issued_at', 'INTEGER', 1, None, 0, 0),
        (7, 'expires_at', 'INTEGER', 1, None, 0, 0),
        (8, 'revoked_at', 'INTEGER', 0, None, 0, 0),
        (9, 'consumed_at', 'INTEGER', 0, None, 0, 0),
        (10, 'consumed_txn', 'TEXT', 0, None, 0, 0),
    ),
)

_AUTHORITY_CORE_INDEXES = (
    (  # authority_install
        (0, 'sqlite_autoindex_authority_install_1', 1, 'u', 0),
    ),
    (  # authority_principal
        (0, 'sqlite_autoindex_authority_principal_1', 1, 'pk', 0),
    ),
    (  # authority_membership
        (0, 'sqlite_autoindex_authority_membership_1', 1, 'pk', 0),
    ),
    (  # authority_right
        (0, 'sqlite_autoindex_authority_right_1', 1, 'pk', 0),
    ),
    (  # authority_delegation
        (0, 'sqlite_autoindex_authority_delegation_1', 1, 'pk', 0),
    ),
    (  # authority_delegation_right
        (0, 'sqlite_autoindex_authority_delegation_right_1', 1, 'pk', 0),
    ),
    (  # authority_delegation_cap
        (0, 'sqlite_autoindex_authority_delegation_cap_1', 1, 'pk', 0),
    ),
    (  # operation_authorization
        (0, 'sqlite_autoindex_operation_authorization_1', 1, 'pk', 0),
    ),
)

_AUTHORITY_CORE_FOREIGN_KEYS = (
    (  # authority_install
    ),
    (  # authority_principal
        (0, 0, 'authority_install', 'install_id', 'install_id', 'RESTRICT', 'RESTRICT', 'NONE'),
    ),
    (  # authority_membership
        (0, 0, 'authority_principal', 'install_id', 'install_id', 'RESTRICT', 'RESTRICT', 'NONE'),
        (0, 1, 'authority_principal', 'principal_id', 'id', 'RESTRICT', 'RESTRICT', 'NONE'),
    ),
    (  # authority_right
        (0, 0, 'authority_principal', 'install_id', 'install_id', 'RESTRICT', 'RESTRICT', 'NONE'),
        (0, 1, 'authority_principal', 'principal_id', 'id', 'RESTRICT', 'RESTRICT', 'NONE'),
    ),
    (  # authority_delegation
        (0, 0, 'authority_principal', 'install_id', 'install_id', 'RESTRICT', 'RESTRICT', 'NONE'),
        (0, 1, 'authority_principal', 'grantee_id', 'id', 'RESTRICT', 'RESTRICT', 'NONE'),
        (1, 0, 'authority_principal', 'install_id', 'install_id', 'RESTRICT', 'RESTRICT', 'NONE'),
        (1, 1, 'authority_principal', 'issuer_id', 'id', 'RESTRICT', 'RESTRICT', 'NONE'),
    ),
    (  # authority_delegation_right
        (0, 0, 'authority_delegation', 'install_id', 'install_id', 'RESTRICT', 'RESTRICT', 'NONE'),
        (0, 1, 'authority_delegation', 'delegation_id', 'id', 'RESTRICT', 'RESTRICT', 'NONE'),
    ),
    (  # authority_delegation_cap
        (0, 0, 'authority_delegation', 'install_id', 'install_id', 'RESTRICT', 'RESTRICT', 'NONE'),
        (0, 1, 'authority_delegation', 'delegation_id', 'id', 'RESTRICT', 'RESTRICT', 'NONE'),
    ),
    (  # operation_authorization
        (0, 0, 'authority_delegation', 'install_id', 'install_id', 'RESTRICT', 'RESTRICT', 'NONE'),
        (0, 1, 'authority_delegation', 'delegation_id', 'id', 'RESTRICT', 'RESTRICT', 'NONE'),
        (1, 0, 'authority_principal', 'install_id', 'install_id', 'RESTRICT', 'RESTRICT', 'NONE'),
        (1, 1, 'authority_principal', 'principal_id', 'id', 'RESTRICT', 'RESTRICT', 'NONE'),
    ),
)

_AUTHORITY_CORE_INDEX_COLUMNS = (
    (  # sqlite_autoindex_authority_delegation_1
        (0, 0, 'install_id', 0, 'BINARY', 1),
        (1, 1, 'id', 0, 'BINARY', 1),
        (2, -1, None, 0, 'BINARY', 0),
    ),
    (  # sqlite_autoindex_authority_delegation_cap_1
        (0, 0, 'install_id', 0, 'BINARY', 1),
        (1, 1, 'delegation_id', 0, 'BINARY', 1),
        (2, 2, 'action', 0, 'BINARY', 1),
        (3, 3, 'currency', 0, 'BINARY', 1),
        (4, 7, 'window_start', 0, 'BINARY', 1),
        (5, 8, 'window_end', 0, 'BINARY', 1),
        (6, -1, None, 0, 'BINARY', 0),
    ),
    (  # sqlite_autoindex_authority_delegation_right_1
        (0, 0, 'install_id', 0, 'BINARY', 1),
        (1, 1, 'delegation_id', 0, 'BINARY', 1),
        (2, 2, 'company_id', 0, 'BINARY', 1),
        (3, 3, 'resource_kind', 0, 'BINARY', 1),
        (4, 4, 'resource_id', 0, 'BINARY', 1),
        (5, 5, 'action', 0, 'BINARY', 1),
        (6, -1, None, 0, 'BINARY', 0),
    ),
    (  # sqlite_autoindex_authority_install_1
        (0, 1, 'install_id', 0, 'BINARY', 1),
        (1, -1, None, 0, 'BINARY', 0),
    ),
    (  # sqlite_autoindex_authority_membership_1
        (0, 0, 'install_id', 0, 'BINARY', 1),
        (1, 1, 'principal_id', 0, 'BINARY', 1),
        (2, 2, 'company_id', 0, 'BINARY', 1),
        (3, 3, 'effect', 0, 'BINARY', 1),
        (4, -1, None, 0, 'BINARY', 0),
    ),
    (  # sqlite_autoindex_authority_principal_1
        (0, 0, 'install_id', 0, 'BINARY', 1),
        (1, 1, 'id', 0, 'BINARY', 1),
        (2, -1, None, 0, 'BINARY', 0),
    ),
    (  # sqlite_autoindex_authority_right_1
        (0, 0, 'install_id', 0, 'BINARY', 1),
        (1, 1, 'principal_id', 0, 'BINARY', 1),
        (2, 2, 'company_id', 0, 'BINARY', 1),
        (3, 3, 'resource_kind', 0, 'BINARY', 1),
        (4, 4, 'resource_id', 0, 'BINARY', 1),
        (5, 5, 'action', 0, 'BINARY', 1),
        (6, 6, 'effect', 0, 'BINARY', 1),
        (7, -1, None, 0, 'BINARY', 0),
    ),
    (  # sqlite_autoindex_operation_authorization_1
        (0, 0, 'id', 0, 'BINARY', 1),
        (1, -1, None, 0, 'BINARY', 0),
    ),
)

_AUTHORITY_CORE_STRUCTURE_DIGEST = (
    "e100762e110f7efea6c4d6fb3d860078fae62f6409afe4c61b97077941bb2b3d"
)

_AUTHORITY_CORE_ROW_COLUMNS = (
    ("singleton", "install_id", "schema_version", "phase", "revision"),
    ("install_id", "id", "kind", "disabled_at"),
    ("install_id", "principal_id", "company_id", "effect"),
    ("install_id", "principal_id", "company_id", "resource_kind",
     "resource_id", "action", "effect"),
    ("install_id", "id", "issuer_id", "grantee_id", "issued_at",
     "expires_at", "revoked_at"),
    ("install_id", "delegation_id", "company_id", "resource_kind",
     "resource_id", "action"),
    ("install_id", "delegation_id", "action", "currency", "scale",
     "per_operation", "aggregate_limit", "window_start", "window_end"),
    ("id", "install_id", "principal_id", "action", "binding_digest",
     "delegation_id", "issued_at", "expires_at", "revoked_at",
     "consumed_at", "consumed_txn"),
)

_AUTHORITY_CORE_ROW_ORDER = (
    ("singleton",),
    ("install_id", "id"),
    ("install_id", "principal_id", "company_id", "effect"),
    ("install_id", "principal_id", "company_id", "resource_kind",
     "resource_id", "action", "effect"),
    ("install_id", "id"),
    ("install_id", "delegation_id", "company_id", "resource_kind",
     "resource_id", "action"),
    ("install_id", "delegation_id", "action", "currency", "window_start",
     "window_end"),
    ("id",),
)

def inspect_authority_core(conn, *, expected_install_id):
    """Same-connection observation of the frozen authority-core profile.

    Checks the expected install identifier first, then accepts only an exact
    wrapper holding an exact native connection whose caller transaction is
    open and whose text decoder is the default. Reads the fixed catalog and
    the complete bounded row sets through the supplied handle only, compares
    every observation to the frozen profile, and returns a read-only mapping
    with exactly profile, status, phase and reason. MATCH carries the stored
    phase; every other outcome carries phase None. A MATCH is an observation
    at one instant: it grants nothing, authenticates nothing, chooses no cap
    and binds no later write.
    """
    import hashlib as _hashlib
    import json as _json
    import sqlite3 as _sqlite3
    import types as _types

    from erpclaw_lib.db import ConnectionWrapper as _ConnectionWrapper

    if type(expected_install_id) is not str:
        raise ValueError("AUTHORITY_CORE_INPUT_INVALID") from None
    if len(expected_install_id) < 1 or len(expected_install_id) > 128:
        raise ValueError("AUTHORITY_CORE_INPUT_INVALID") from None
    for _letter in expected_install_id:
        if "a" <= _letter <= "z" or "A" <= _letter <= "Z" \
                or "0" <= _letter <= "9" or _letter == "_" \
                or _letter == "-":
            continue
        raise ValueError("AUTHORITY_CORE_INPUT_INVALID") from None
    if type(conn) is not _ConnectionWrapper:
        raise ValueError("AUTHORITY_CORE_UNSUPPORTED") from None
    try:
        raw = object.__getattribute__(conn, "_conn")
    except Exception:
        raise ValueError("AUTHORITY_CORE_UNSUPPORTED") from None
    if type(raw) is not _sqlite3.Connection:
        raise ValueError("AUTHORITY_CORE_UNSUPPORTED") from None
    try:
        _decoder = raw.text_factory
        _in_txn = raw.in_transaction
        _isolation = raw.isolation_level
    except Exception:
        raise RuntimeError("AUTHORITY_CORE_STORAGE_ERROR") from None
    if _decoder is not str:
        raise ValueError("AUTHORITY_CORE_UNSUPPORTED") from None
    if _in_txn is not True or _isolation is None:
        raise ValueError("AUTHORITY_CORE_TRANSACTION_REQUIRED") from None

    _opened = []
    _close_failed = []

    def _result(_status, _phase, _reason):
        return _types.MappingProxyType({
            "profile": _AUTHORITY_CORE_PROFILE,
            "status": _status,
            "phase": _phase,
            "reason": _reason,
        })

    def _read_all(_statement):
        try:
            _cursor = raw.cursor()
        except Exception:
            raise RuntimeError("AUTHORITY_CORE_STORAGE_ERROR") from None
        _opened.append(_cursor)
        try:
            _cursor.row_factory = None
            _cursor.execute(_statement)
        except Exception:
            raise RuntimeError("AUTHORITY_CORE_STORAGE_ERROR") from None
        try:
            _rows = _cursor.fetchall()
        except Exception:
            raise RuntimeError("AUTHORITY_CORE_STORAGE_ERROR") from None
        return _rows

    def _shaped(_rows, _arity):
        if type(_rows) is not list:
            raise RuntimeError("AUTHORITY_CORE_STORAGE_ERROR") from None
        for _record in _rows:
            if type(_record) is not tuple or len(_record) != _arity:
                raise RuntimeError("AUTHORITY_CORE_STORAGE_ERROR") from None
        return tuple(_rows)

    def _cells(_rows, _kinds):
        for _record in _rows:
            for _value, _kind in zip(_record, _kinds):
                if _kind == "int":
                    if type(_value) is not int:
                        raise RuntimeError(
                            "AUTHORITY_CORE_STORAGE_ERROR") from None
                elif _kind == "str":
                    if type(_value) is not str:
                        raise RuntimeError(
                            "AUTHORITY_CORE_STORAGE_ERROR") from None
                elif _value is not None and type(_value) is not str:
                    raise RuntimeError(
                        "AUTHORITY_CORE_STORAGE_ERROR") from None

    def _is_id(_value):
        if type(_value) is not str:
            return False
        if len(_value) < 1 or len(_value) > 128:
            return False
        for _letter in _value:
            if "a" <= _letter <= "z" or "A" <= _letter <= "Z" \
                    or "0" <= _letter <= "9" or _letter == "_" \
                    or _letter == "-":
                continue
            return False
        return True

    def _is_action(_value):
        if type(_value) is not str:
            return False
        if len(_value) < 1 or len(_value) > 128:
            return False
        if not ("a" <= _value[0] <= "z"):
            return False
        for _letter in _value[1:]:
            if "a" <= _letter <= "z" or "0" <= _letter <= "9" \
                    or _letter == "-":
                continue
            return False
        return True

    def _is_epoch(_value):
        return type(_value) is int and 0 <= _value <= _AUTHORITY_CORE_INT_MAX

    def _is_currency(_value):
        if type(_value) is not str or len(_value) != 3:
            return False
        for _letter in _value:
            if not ("A" <= _letter <= "Z"):
                return False
        return True

    def _is_int_part(_text):
        if _text == "0":
            return True
        if type(_text) is not str or len(_text) < 1:
            return False
        if not ("1" <= _text[0] <= "9"):
            return False
        for _letter in _text[1:]:
            if not ("0" <= _letter <= "9"):
                return False
        return True

    def _is_amount(_value, _scale):
        if type(_value) is not str or type(_scale) is not int:
            return False
        if len(_value) < 1 or len(_value) > 128:
            return False
        if _scale < 0 or _scale > 18:
            return False
        if _scale == 0:
            return _is_int_part(_value)
        _parts = _value.split(".")
        if len(_parts) != 2:
            return False
        _head, _tail = _parts
        if len(_tail) != _scale or not _is_int_part(_head):
            return False
        for _letter in _tail:
            if not ("0" <= _letter <= "9"):
                return False
        return True

    def _is_digest(_value):
        if type(_value) is not str or len(_value) != 64:
            return False
        for _letter in _value:
            if "0" <= _letter <= "9" or "a" <= _letter <= "f":
                continue
            return False
        return True

    def _tag(_value):
        if _value is None:
            return ["none", 0]
        if type(_value) is str:
            return ["str", _value]
        if type(_value) is int:
            return ["int", _value]
        if type(_value) is tuple or type(_value) is list:
            return ["tuple", [_tag(_item) for _item in _value]]
        if type(_value) is dict:
            return ["map", [[_key, _tag(_value[_key])]
                            for _key in sorted(_value.keys())]]
        raise ValueError("untagged observation")

    def _run():
        try:
            _evidence = authority_core_schema_evidence(conn)
        except RuntimeError as _exc:
            if _exc.args == ("AUTHORITY_CORE_INCOMPLETE",):
                return _result("INCOMPLETE", None, "LIMIT")  # catalog budget
            if _exc.args in (("AUTHORITY_CORE_STORAGE_ERROR",),
                             ("AUTHORITY_CORE_STORAGE_INVALID",)):
                raise RuntimeError("AUTHORITY_CORE_STORAGE_ERROR") from None
            raise
        _fk_rows = _shaped(_read_all(_AUTHORITY_CORE_FK_STATE_SQL), 1)
        _fk_on = (
            len(_fk_rows) == 1 and _fk_rows[0] == (1,)
            and type(_fk_rows[0][0]) is int
        )
        _db_rows = _shaped(_read_all(_AUTHORITY_CORE_DB_LIST_SQL), 3)
        _db_names = []
        for _entry in _db_rows:
            _seq, _name, _file = _entry
            if type(_seq) is not int or type(_name) is not str \
                    or type(_file) is not str:
                raise RuntimeError("AUTHORITY_CORE_STORAGE_ERROR") from None
            _db_names.append(_name)
        _fixed_lower = frozenset(
            _label.lower() for _label in _AUTHORITY_CORE_TABLES)
        _temp_collision = False
        for _item in _evidence:
            if _item[0] != "temp":
                continue
            if _item[2].lower() in _fixed_lower \
                    or _item[3].lower() in _fixed_lower:
                _temp_collision = True
                break
        if not _fk_on:
            return _result("MISMATCH", None, "NAMESPACE")
        _namespace_ok = (
            "main" in _db_names
            and all(_name == "main" or _name == "temp"
                    for _name in _db_names)
            and not _temp_collision
        )
        if not _namespace_ok:
            return _result("MISMATCH", None, "NAMESPACE")
        _main_rows = tuple(
            _item for _item in _evidence if _item[0] == "main")
        try:
            _main_canon = _json.dumps(
                _tag(_main_rows), sort_keys=True, separators=(",", ":"),
                ensure_ascii=True, allow_nan=False)
            _main_digest = _hashlib.sha256(
                _main_canon.encode("utf-8")).hexdigest()
        except Exception:
            return _result("MISMATCH", None, "STRUCTURE")
        if _main_digest != _AUTHORITY_CORE_RAW_MAIN_DIGEST:
            return _result("MISMATCH", None, "STRUCTURE")
        if _sqlite3.sqlite_version != _AUTHORITY_CORE_SQLITE_VERSION:
            return _result("MISMATCH", None, "STRUCTURE")
        _live_columns = []
        _live_indexes = []
        _live_fkeys = []
        for _shape_pos in range(len(_AUTHORITY_CORE_TABLES)):
            _shape_table = _AUTHORITY_CORE_TABLES[_shape_pos]
            _shape = _shaped(
                _read_all(_AUTHORITY_CORE_TABLE_SHAPE_SQL % _shape_table), 7)
            _listing = _shaped(
                _read_all(_AUTHORITY_CORE_INDEX_LIST_SQL % _shape_table), 5)
            _links = _shaped(
                _read_all(_AUTHORITY_CORE_FOREIGN_KEY_SQL % _shape_table), 8)
            _cells(_shape, ("int", "str", "str", "int", "opt", "int", "int"))
            _cells(_listing, ("int", "str", "int", "str", "int"))
            _cells(_links, ("int", "int", "str", "str", "str", "str",
                            "str", "str"))
            _live_columns.append(_shape)
            _live_indexes.append(_listing)
            _live_fkeys.append(_links)
        _seen_names = frozenset(
            _record[1] for _rows in _live_indexes for _record in _rows)
        if _seen_names != frozenset(_AUTHORITY_CORE_AUTO_INDEXES):
            return _result("MISMATCH", None, "STRUCTURE")
        _live_details = []
        for _index_name in _AUTHORITY_CORE_AUTO_INDEXES:
            _detail = _shaped(
                _read_all(_AUTHORITY_CORE_INDEX_DETAIL_SQL % _index_name), 6)
            _cells(_detail, ("int", "int", "opt", "int", "str", "int"))
            _live_details.append(_detail)
        _live_bundle = {
            "columns": dict(zip(_AUTHORITY_CORE_TABLES, _live_columns)),
            "foreign_keys": dict(zip(_AUTHORITY_CORE_TABLES, _live_fkeys)),
            "index_columns": dict(
                zip(_AUTHORITY_CORE_AUTO_INDEXES, _live_details)),
            "indexes": dict(zip(_AUTHORITY_CORE_TABLES, _live_indexes)),
            "profile": _AUTHORITY_CORE_PROFILE,
            "provenance": {"sqlite": _sqlite3.sqlite_version},
        }
        try:
            _canon = _json.dumps(
                _tag(_live_bundle), sort_keys=True, separators=(",", ":"),
                ensure_ascii=True, allow_nan=False)
            _live_digest = _hashlib.sha256(
                _canon.encode("utf-8")).hexdigest()
        except Exception:
            return _result("MISMATCH", None, "STRUCTURE")
        if _live_digest != _AUTHORITY_CORE_STRUCTURE_DIGEST:
            return _result("MISMATCH", None, "STRUCTURE")
        _fetched = []
        _row_total = 0
        _text_total = 0
        _exhausted = False
        for _fetch_pos in range(len(_AUTHORITY_CORE_TABLES)):
            _fetch_table = _AUTHORITY_CORE_TABLES[_fetch_pos]
            _fetch_cols = _AUTHORITY_CORE_ROW_COLUMNS[_fetch_pos]
            _fetch_order = _AUTHORITY_CORE_ROW_ORDER[_fetch_pos]
            _select = "SELECT " + ", ".join(
                "\"%s\"" % _column for _column in _fetch_cols) \
                + " FROM \"%s\"" % _fetch_table + " ORDER BY " + ", ".join(
                    "\"%s\"" % _column for _column in _fetch_order)
            try:
                _read_cursor = raw.cursor()
            except Exception:
                raise RuntimeError("AUTHORITY_CORE_STORAGE_ERROR") from None
            _opened.append(_read_cursor)
            try:
                _read_cursor.row_factory = None
                _read_cursor.execute(_select)
            except Exception:
                raise RuntimeError("AUTHORITY_CORE_STORAGE_ERROR") from None
            _table_rows = []
            while True:
                try:
                    _batch = _read_cursor.fetchmany(
                        _AUTHORITY_CORE_FETCH_BATCH)
                except Exception:
                    raise RuntimeError(
                        "AUTHORITY_CORE_STORAGE_ERROR") from None
                if not _batch:
                    break
                for _record in _batch:
                    if type(_record) is not tuple \
                            or len(_record) != len(_fetch_cols):
                        raise RuntimeError(
                            "AUTHORITY_CORE_STORAGE_ERROR") from None
                    _row_total += 1
                    if _row_total > _AUTHORITY_CORE_INSPECT_ROW_LIMIT:
                        _exhausted = True
                        break
                    try:
                        for _field in _record:
                            if _field is None:
                                continue
                            if type(_field) is str:
                                _text_total += len(
                                    _field.encode("utf-8"))
                            elif type(_field) is bytes:
                                _text_total += len(_field)
                            else:
                                _text_total += len(
                                    str(_field).encode("utf-8"))
                            if _text_total > \
                                    _AUTHORITY_CORE_INSPECT_TEXT_LIMIT:
                                _exhausted = True
                                break
                    except Exception:
                        raise RuntimeError(
                            "AUTHORITY_CORE_STORAGE_ERROR") from None
                    if _exhausted:
                        break
                    _table_rows.append(_record)
                if _exhausted:
                    break
            _fetched.append(tuple(_table_rows))
            if _exhausted:
                break
        if _exhausted:
            return _result("INCOMPLETE", None, "LIMIT")  # row budget spent
        _install_rows = _fetched[0]
        if len(_install_rows) != 1:
            return _result("MISMATCH", None, "INSTALL")
        _singleton, _row_install, _schema_version, _phase, _revision = \
            _install_rows[0]
        if type(_singleton) is not int or _singleton != 1:
            return _result("MISMATCH", None, "INSTALL")
        if type(_schema_version) is not int or _schema_version != 1:
            return _result("MISMATCH", None, "INSTALL")
        if type(_row_install) is not str \
                or _row_install != expected_install_id:
            return _result("MISMATCH", None, "INSTALL")
        if _phase != "STAGED" and _phase != "ACTIVE":
            return _result("MISMATCH", None, "INSTALL")
        if type(_revision) is not int or _revision < 0 \
                or _revision > _AUTHORITY_CORE_INT_MAX:
            return _result("MISMATCH", None, "INSTALL")
        _principals = set()
        for _record in _fetched[1]:
            _r_install, _r_id, _r_kind, _r_disabled = _record
            if type(_r_install) is not str \
                    or _r_install != expected_install_id:
                return _result("MISMATCH", None, "ROWS")
            if not _is_id(_r_id):
                return _result("MISMATCH", None, "ROWS")
            if _r_kind != "human" and _r_kind != "service":
                return _result("MISMATCH", None, "ROWS")
            if _r_disabled is not None and not _is_epoch(_r_disabled):
                return _result("MISMATCH", None, "ROWS")
            _principals.add((_r_install, _r_id))
        for _record in _fetched[2]:
            _r_install, _r_principal, _r_company, _r_effect = _record
            if type(_r_install) is not str \
                    or _r_install != expected_install_id:
                return _result("MISMATCH", None, "ROWS")
            if not _is_id(_r_principal) or not _is_id(_r_company):
                return _result("MISMATCH", None, "ROWS")
            if _r_effect != "allow" and _r_effect != "deny":
                return _result("MISMATCH", None, "ROWS")
            if (_r_install, _r_principal) not in _principals:
                return _result("MISMATCH", None, "ROWS")
        for _record in _fetched[3]:
            (_r_install, _r_principal, _r_company, _r_kind, _r_resource,
             _r_action, _r_effect) = _record
            if type(_r_install) is not str \
                    or _r_install != expected_install_id:
                return _result("MISMATCH", None, "ROWS")
            if not _is_id(_r_principal) or not _is_id(_r_company):
                return _result("MISMATCH", None, "ROWS")
            if not _is_action(_r_kind) or not _is_id(_r_resource):
                return _result("MISMATCH", None, "ROWS")
            if not _is_action(_r_action):
                return _result("MISMATCH", None, "ROWS")
            if _r_effect != "allow" and _r_effect != "deny":
                return _result("MISMATCH", None, "ROWS")
            if (_r_install, _r_principal) not in _principals:
                return _result("MISMATCH", None, "ROWS")
        _delegations = set()
        for _record in _fetched[4]:
            (_r_install, _r_id, _r_issuer, _r_grantee, _r_issued,
             _r_expires, _r_revoked) = _record
            if type(_r_install) is not str \
                    or _r_install != expected_install_id:
                return _result("MISMATCH", None, "ROWS")
            if not _is_id(_r_id) or not _is_id(_r_issuer):
                return _result("MISMATCH", None, "ROWS")
            if not _is_id(_r_grantee):
                return _result("MISMATCH", None, "ROWS")
            if _r_issuer == _r_grantee:
                return _result("MISMATCH", None, "ROWS")
            if not _is_epoch(_r_issued) or not _is_epoch(_r_expires):
                return _result("MISMATCH", None, "ROWS")
            if not _r_issued < _r_expires:
                return _result("MISMATCH", None, "ROWS")
            if _r_revoked is not None and not _is_epoch(_r_revoked):
                return _result("MISMATCH", None, "ROWS")
            if (_r_install, _r_issuer) not in _principals:
                return _result("MISMATCH", None, "ROWS")
            if (_r_install, _r_grantee) not in _principals:
                return _result("MISMATCH", None, "ROWS")
            _delegations.add((_r_install, _r_id))
        for _record in _fetched[5]:
            (_r_install, _r_delegation, _r_company, _r_kind, _r_resource,
             _r_action) = _record
            if type(_r_install) is not str \
                    or _r_install != expected_install_id:
                return _result("MISMATCH", None, "ROWS")
            if not _is_id(_r_delegation) or not _is_id(_r_company):
                return _result("MISMATCH", None, "ROWS")
            if not _is_action(_r_kind) or not _is_id(_r_resource):
                return _result("MISMATCH", None, "ROWS")
            if not _is_action(_r_action):
                return _result("MISMATCH", None, "ROWS")
            if (_r_install, _r_delegation) not in _delegations:
                return _result("MISMATCH", None, "ROWS")
        for _record in _fetched[6]:
            (_r_install, _r_delegation, _r_action, _r_currency, _r_scale,
             _r_per_operation, _r_aggregate, _r_start, _r_end) = _record
            if type(_r_install) is not str \
                    or _r_install != expected_install_id:
                return _result("MISMATCH", None, "ROWS")
            if not _is_id(_r_delegation):
                return _result("MISMATCH", None, "ROWS")
            if not _is_action(_r_action):
                return _result("MISMATCH", None, "ROWS")
            if not _is_currency(_r_currency):
                return _result("MISMATCH", None, "ROWS")
            if type(_r_scale) is not int or _r_scale < 0 or _r_scale > 18:
                return _result("MISMATCH", None, "ROWS")
            if not _is_amount(_r_per_operation, _r_scale):
                return _result("MISMATCH", None, "ROWS")
            if not _is_amount(_r_aggregate, _r_scale):
                return _result("MISMATCH", None, "ROWS")
            if not _is_epoch(_r_start) or not _is_epoch(_r_end):
                return _result("MISMATCH", None, "ROWS")
            if not _r_start < _r_end:
                return _result("MISMATCH", None, "ROWS")
            if (_r_install, _r_delegation) not in _delegations:
                return _result("MISMATCH", None, "ROWS")
        for _record in _fetched[7]:
            (_r_id, _r_install, _r_principal, _r_action, _r_digest,
             _r_delegation, _r_issued, _r_expires, _r_revoked,
             _r_consumed_at, _r_consumed_txn) = _record
            if not _is_id(_r_id):
                return _result("MISMATCH", None, "ROWS")
            if type(_r_install) is not str \
                    or _r_install != expected_install_id:
                return _result("MISMATCH", None, "ROWS")
            if not _is_id(_r_principal):
                return _result("MISMATCH", None, "ROWS")
            if not _is_action(_r_action):
                return _result("MISMATCH", None, "ROWS")
            if not _is_digest(_r_digest):
                return _result("MISMATCH", None, "ROWS")
            if _r_delegation is not None:
                if not _is_id(_r_delegation):
                    return _result("MISMATCH", None, "ROWS")
                if (_r_install, _r_delegation) not in _delegations:
                    return _result("MISMATCH", None, "ROWS")
            if not _is_epoch(_r_issued) or not _is_epoch(_r_expires):
                return _result("MISMATCH", None, "ROWS")
            if not _r_expires >= _r_issued:
                return _result("MISMATCH", None, "ROWS")
            if _r_revoked is not None and not _is_epoch(_r_revoked):
                return _result("MISMATCH", None, "ROWS")
            if _r_consumed_at is None and _r_consumed_txn is None:
                pass
            elif _is_epoch(_r_consumed_at) and _is_id(_r_consumed_txn):
                pass
            else:
                return _result("MISMATCH", None, "ROWS")
            if (_r_install, _r_principal) not in _principals:
                return _result("MISMATCH", None, "ROWS")
        return _result("MATCH", _phase, "MATCH")

    try:
        _outcome = _run()
    finally:
        for _cursor in _opened:
            try:
                _cursor.close()
            except Exception:
                _close_failed.append(_cursor)
    if _close_failed:
        raise RuntimeError("AUTHORITY_CORE_STORAGE_ERROR") from None
    return _outcome
