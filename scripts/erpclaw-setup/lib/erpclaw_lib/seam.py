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
    setup_pragmas, _DecimalSum, db_error_types,
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
    path = os.path.abspath(os.path.expanduser(
        db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)))
    # Same courtesy get_connection extends: create the parent directory. Phase 2
    # provisions databases through this path, and SQLite reports a missing parent
    # as the thoroughly unhelpful "unable to open database file".
    ensure_db_exists(path)
    return "sqlite:///" + path


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
    "ForeignKey", "UniqueConstraint", "PrimaryKeyConstraint", "text",
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
            "index_defs": index_definitions(table, db_path)}


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
