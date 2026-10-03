"""Database connection helper for ERPClaw.

Provides a standard way to get a database connection with the correct
connection settings applied for the active dialect:

  - SQLite (default): PRAGMAs (WAL mode, FK enforcement, busy timeout),
    the ``decimal_sum`` aggregate, and ``sqlite3.Row`` row access.
  - PostgreSQL (``ERPCLAW_DB_DIALECT=postgresql``): a psycopg2 connection
    with ``lock_timeout`` / ``statement_timeout`` set and a ``DictCursor``
    so rows support both positional (``row[0]``) and key (``row['col']``)
    access — matching ``sqlite3.Row`` semantics. The returned wrapper exposes
    a SQLite-style ``conn.execute(sql, params)`` API, translating ``?``
    placeholders to psycopg2's ``%s`` so existing call sites work unchanged.

Dialect is selected by ``ERPCLAW_DB_DIALECT`` (``sqlite`` | ``postgresql``).
The Postgres connection URL is resolved from the ``db_path`` argument, then ``ERPCLAW_DB_URL``, then ``ERPCLAW_DB_PATH``. The migration runner resolves its own target in a different order (``ERPCLAW_DB_URL`` first) and passes that target explicitly.

When ``ERPCLAW_DB_READONLY`` is set to ``1``, every connection opened here refuses writes and creates nothing on disk or on the server. On SQLite, "creates nothing" holds for a rollback-journal (DELETE mode) file or a file on a read-only mount: a ``mode=ro`` open of a WAL-mode file in a writable directory may still create its ``-shm`` side file. PostgreSQL's read-only session setting can be reversed by SQL on the same connection, so a PostgreSQL source for a read-only session needs a role without write grants as well. Unset or empty keeps today's behaviour, and any other value is a configuration error that refuses the connection without echoing the value.
"""
import os
import sqlite3
import stat
import sys
import time
from decimal import Decimal

from erpclaw_lib.paths import db_default


# Default SQLite path, derived from ERPCLAW_HOME (ADR-0017). With ERPCLAW_HOME
# unset this equals os.path.expanduser("~/.openclaw/erpclaw/data.sqlite") exactly.
# The ERPCLAW_DB_URL / ERPCLAW_DB_PATH chain below remains the DB-location
# authority; this is only the default underneath it.
DEFAULT_DB_PATH = db_default()


def integrity_error_types():
    """Return integrity-error classes for the active dialect.

    Returns a tuple holding SQLite's integrity-error class always, plus
    the PostgreSQL driver's integrity-error class only when the active
    dialect is PostgreSQL (imported inside the function, so importing
    this module never pulls in the driver on other backends). Product
    code catches the returned tuple to refuse unique violations with one
    portable handler that stays narrower than the base error class.
    """
    if get_dialect() == "postgresql":
        import psycopg2
        return (sqlite3.IntegrityError, psycopg2.IntegrityError)
    return (sqlite3.IntegrityError,)


def get_dialect():
    """Return the configured database dialect."""
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def readonly_requested() -> bool:
    """Whether the process asked for read-only storage."""
    value = os.environ.get("ERPCLAW_DB_READONLY")
    if value is None or value == "":
        return False
    if value == "1":
        return True
    raise RuntimeError("ERPCLAW_DB_READONLY must be unset or 1")


def readonly_sqlite_uri(path) -> str:
    """The ``file:...?mode=ro`` URI for a SQLite path, percent-encoded.

    Encoding the absolute filesystem path keeps spaces, ``?``, ``#`` and
    ``%`` in a name from reshaping the URI or adding parameters.
    """
    from urllib.parse import quote as _quote
    absolute = os.path.abspath(os.fspath(path))
    return "file:" + _quote(absolute, safe="/") + "?mode=ro"


def _apply_readonly(conn) -> None:
    """Apply the read-only setting to an open connection."""
    if get_dialect() == "postgresql":
        cur = conn.cursor() if hasattr(conn, "cursor") else conn
        try:
            cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        finally:
            if cur is not conn:
                cur.close()
    else:
        conn.execute("PRAGMA query_only=ON")


def db_error_types():
    """Return DB-API exception classes for the active dialect.

    Returns a ``(missing_table_excs, db_error_base)`` tuple:
      - ``missing_table_excs``: tuple of exception classes raised when a
        table or relation does not exist. Tolerated on minimal installs
        where an optional table (e.g. an audit/compliance log) has not
        been created yet.
      - ``db_error_base``: the dialect's PEP 249 base error class, used to
        catch any *other* database failure so it can be surfaced rather
        than silently swallowed.

    Both sqlite3 and psycopg2 implement the PEP 249 DB-API but expose
    distinct exception classes, so callers must NOT hardcode ``sqlite3.*``
    when ``ERPCLAW_DB_DIALECT=postgresql``. On SQLite a missing table raises
    ``OperationalError`` ("no such table"); on PostgreSQL it raises
    ``psycopg2.errors.UndefinedTable`` (a ``ProgrammingError``,
    SQLSTATE 42P01). The missing-table classes subclass the base error
    class in both drivers, so callers must order their ``except`` clauses
    most-specific first.
    """
    if get_dialect() == "postgresql":
        import psycopg2
        from psycopg2 import errors as _pg_errors
        return (_pg_errors.UndefinedTable,), psycopg2.Error
    return (sqlite3.OperationalError,), sqlite3.Error


def db_integrity_error(conn=None):
    """Return the integrity-error class for the connection in use.

    Prefers the DB-API connection attribute (``conn.IntegrityError``), which
    both connection wrappers pass through to the underlying driver, so the
    caller catches exactly what its own connection raises. Falls back to the
    active dialect's driver class, chosen the way :func:`db_error_types`
    chooses (PostgreSQL driver imported lazily), when no connection is given
    or it exposes no such attribute.
    """
    if conn is not None:
        try:
            return conn.IntegrityError
        except AttributeError:
            pass
    if get_dialect() == "postgresql":
        from psycopg2 import IntegrityError as _PgIntegrityError
        return _PgIntegrityError
    return sqlite3.IntegrityError


def is_lock_conflict(exc):
    """Whether an exception is a lock conflict with nothing written.

    True for a PostgreSQL deadlock or lock-not-available error and for
    SQLite's locked-database error, false otherwise. PostgreSQL errors
    are recognised through the driver's error classes, imported lazily
    the way the other helpers here import the driver, so importing this
    module never pulls in the driver on other backends.
    """
    try:
        from psycopg2 import errors as _pg_errors
        if isinstance(exc, (_pg_errors.DeadlockDetected,
                            _pg_errors.LockNotAvailable)):
            return True
    except ImportError:
        pass
    if isinstance(exc, sqlite3.OperationalError):
        return "database is locked" in str(exc).lower()
    return False


_READONLY_REFUSED = ("This session is read-only; the action needed to write, "
                     "so nothing was written.")
_READONLY_FILE = ("The database refused the write because it is read-only; "
                  "nothing was written.")
_UNEXPECTED = "An unexpected error occurred"
# PostgreSQL's SQLSTATE for "cannot execute ... in a read-only transaction".
_PG_READ_ONLY_SQL_TRANSACTION = "25006"


def is_readonly_refusal(exc):
    """Whether an exception is the database refusing a write in read-only mode.

    True for SQLite's read-only refusal (result code SQLITE_READONLY or any
    of its extended codes; "attempt to write a readonly database") and for
    PostgreSQL's read-only transaction error (by class or by SQLSTATE 25006),
    false otherwise. A PostgreSQL error can only exist once its driver is
    loaded, so the driver is looked up among loaded modules and never
    imported here.
    """
    if "psycopg2" in sys.modules:
        _pg_errors = sys.modules.get("psycopg2.errors")
        if _pg_errors is not None and isinstance(
                exc, _pg_errors.ReadOnlySqlTransaction):
            return True
        if getattr(exc, "pgcode", None) == _PG_READ_ONLY_SQL_TRANSACTION:
            return True
    if isinstance(exc, sqlite3.OperationalError):
        # By result code where the driver gives one (Python 3.11+: the
        # primary code is the low byte of the extended one), by SQLite's
        # message otherwise, so a rewording cannot hide the refusal.
        code = getattr(exc, "sqlite_errorcode", None)
        if code is not None and code & 0xFF == sqlite3.SQLITE_READONLY:
            return True
        return "readonly database" in str(exc).lower()
    return False


def unexpected_error_message(exc):
    """The message a domain script's last-resort handler prints for ``exc``.

    A read-only refusal is named as such: as the session's when the process
    asked for read-only storage, otherwise as the database's own (a
    read-only file or server). Anything else keeps the generic message, so
    no internal detail reaches the answer.
    """
    if is_readonly_refusal(exc):
        try:
            session = readonly_requested()
        except RuntimeError:
            session = False
        return _READONLY_REFUSED if session else _READONLY_FILE
    return _UNEXPECTED


def _enable_wal(conn) -> None:
    """Switch a writable SQLite connection to WAL, unless the file is read-only.

    A database file this process cannot write (a 0444 file, or one owned by
    another user) is opened read-only by the driver for the life of the
    handle, and SQLite refuses the journal-mode change on it. Such a file keeps
    its own journal mode: reads answer as they would from a WAL file, and
    every write is refused by the database, which the domain scripts'
    last-resort handlers name as a read-only file. Any other error is raised.
    """
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as exc:
        if not is_readonly_refusal(exc):
            raise


def setup_pragmas(conn):
    """Apply vendor-specific connection settings.

    For SQLite: WAL mode, FK enforcement, busy timeout.
    For PostgreSQL: lock timeout + statement timeout (via cursor for psycopg2
      compatibility). ``lock_timeout`` is the direct analogue of SQLite's
      ``busy_timeout`` (how long to wait for a lock before erroring);
      ``statement_timeout`` has no SQLite equivalent so it defaults to 0
      (unlimited), but the statement is still issued per the cross-DB plan.
      Both are overridable via ``ERPCLAW_PG_LOCK_TIMEOUT`` /
      ``ERPCLAW_PG_STATEMENT_TIMEOUT``.
    Supported backends are SQLite and PostgreSQL only (MySQL ruled out
    2026-08-11 — see erpclaw_lib/query.py's SUPPORTED_DIALECTS note).

    When read-only mode is requested, the SQLite branch keeps foreign-key and
    busy-timeout settings but skips the journal-mode change, then applies the
    connection-local read-only setting. The PostgreSQL branch applies its
    timeouts first and then applies the session read-only setting.
    """
    dialect = get_dialect()
    if dialect == "sqlite":
        read_only = readonly_requested()
        if not read_only:
            _enable_wal(conn)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        if read_only:
            _apply_readonly(conn)
    elif dialect == "postgresql":
        read_only = readonly_requested()
        lock_timeout = os.environ.get("ERPCLAW_PG_LOCK_TIMEOUT", "5s")
        statement_timeout = os.environ.get("ERPCLAW_PG_STATEMENT_TIMEOUT", "0")
        cur = conn.cursor() if hasattr(conn, 'cursor') else conn
        try:
            cur.execute("SET lock_timeout = %s", (lock_timeout,))
            cur.execute("SET statement_timeout = %s", (statement_timeout,))
        finally:
            if cur is not conn:
                cur.close()
        if read_only:
            _apply_readonly(conn)


class _DecimalSum:
    """Custom SQLite aggregate: SUM using Python Decimal for precision.

    SQLite's built-in SUM uses IEEE 754 float, which can lose precision
    on financial amounts stored as TEXT. This aggregate sums values using
    Python's Decimal type and returns the result as TEXT.

    Usage in SQL: decimal_sum(column) instead of SUM(CAST(column AS REAL))
    """

    def __init__(self):
        self.total = Decimal("0")

    def step(self, value):
        if value is not None:
            self.total += Decimal(str(value))

    def finalize(self):
        return str(self.total)


LEDGER_SINK_NAMES = frozenset({
    "gl_entry",
    "gl_chain_head",
    "stock_ledger_entry",
    "stock_fifo_layer",
    "payment_ledger_entry",
})


def _check_ledger_write(wrapper, sql):
    text = sql if isinstance(sql, str) else str(sql)
    low = text.lower()
    if "u&\"" not in low and not any(
            name in low for name in LEDGER_SINK_NAMES):
        return
    import erpclaw_lib.authority_sink as _authority_sink
    _authority_sink.check_statement(wrapper, sql)


class ConnectionWrapper:
    """Wrapper around sqlite3.Connection that allows setting custom attributes.

    Python 3.12+ disallows setting arbitrary attributes on sqlite3.Connection.
    This wrapper delegates all sqlite3 methods to the underlying connection
    while allowing custom attributes (e.g., conn.company_id) that ERPClaw
    skills use for naming series resolution.
    """

    def __init__(self, conn: sqlite3.Connection):
        object.__setattr__(self, "_conn", conn)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        try:
            setattr(self._conn, name, value)
        except AttributeError:
            object.__setattr__(self, name, value)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *args):
        import erpclaw_lib.authority_sink as _authority_sink
        return _authority_sink.exit_wrapper(self, args)

    def __call__(self, *args, **kwargs):
        return self._conn(*args, **kwargs)

    def execute(self, sql, *args):
        import erpclaw_lib.authority_sink as _authority_sink
        _authority_sink.guard_statement(self, sql)
        _check_ledger_write(self, sql)
        return self._conn.execute(sql, *args)

    def executemany(self, sql, *args):
        import erpclaw_lib.authority_sink as _authority_sink
        _authority_sink.guard_statement(self, sql)
        _check_ledger_write(self, sql)
        return self._conn.executemany(sql, *args)

    def executescript(self, script):
        import erpclaw_lib.authority_sink as _authority_sink
        _authority_sink.guard_statement(self, script)
        _authority_sink.check_executescript(self, script)
        return self._conn.executescript(script)

    def commit(self):
        import erpclaw_lib.authority_sink as _authority_sink
        return _authority_sink.commit_wrapper(self)

    def rollback(self):
        import erpclaw_lib.authority_sink as _authority_sink
        return _authority_sink.rollback_wrapper(self)

    def close(self):
        import erpclaw_lib.authority_sink as _authority_sink
        return _authority_sink.close_wrapper(self)


def _qmark_to_pyformat(sql: str) -> str:
    """Translate SQLite qmark (``?``) placeholders to psycopg2 pyformat (``%s``).

    The codebase builds parameterized SQL with PyPika's ``QmarkParameter`` (``?``);
    psycopg2 expects ``%s``. Translation rules, in a single left-to-right scan
    that tracks single-quoted string literals:

      - A ``?`` OUTSIDE a string literal becomes ``%s``.
      - A ``?`` INSIDE a string literal is left untouched (it's data, not a
        placeholder).
      - Every literal ``%`` is doubled to ``%%`` — when params are supplied,
        psycopg2 runs its own %-substitution over the whole query string, so a
        bare ``%`` (e.g. inside a ``LIKE '%foo%'``) would be mis-parsed.

    Only call this when params are actually being passed; with no params
    psycopg2 does not %-process the query, so ``%`` must be left alone.
    """
    out = []
    in_str = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            # A doubled '' is an escaped quote inside a string literal; emit both
            # and stay in the same state.
            if in_str and i + 1 < n and sql[i + 1] == "'":
                out.append("''")
                i += 2
                continue
            in_str = not in_str
            out.append(ch)
        elif ch == "%":
            out.append("%%")
        elif ch == "?" and not in_str:
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


class PgConnectionWrapper:
    """SQLite-style facade over a psycopg2 connection.

    Domain code is written against ``sqlite3.Connection``: it calls
    ``conn.execute(sql, params)`` directly (sqlite3 returns a cursor),
    iterates rows, and sometimes sets bookkeeping attributes such as
    ``conn.company_id``. psycopg2 connections have none of that — you go
    through ``conn.cursor()`` and use ``%s`` placeholders.

    This wrapper bridges the gap so ``get_connection()`` returns a drop-in
    object for both backends:

      - ``execute`` / ``executemany`` open a cursor (``DictCursor`` via the
        connection's ``cursor_factory``), translate ``?`` → ``%s``, run the
        statement, and return the cursor (which supports ``fetchone`` /
        ``fetchall`` and yields rows that index by position and by name).
      - ``commit`` / ``rollback`` / ``close`` / ``cursor`` and the context
        manager delegate to the underlying psycopg2 connection, whose
        ``with`` semantics (commit on success, rollback on error, no close)
        already match sqlite3.
      - Arbitrary attributes set on the wrapper that psycopg2 rejects are
        stored on the wrapper itself (mirrors ``ConnectionWrapper``).

    Note: the ``decimal_sum`` aggregate is registered as a persistent SQL
    aggregate by :func:`_ensure_pg_decimal_sum` during ``get_connection`` (it
    cannot be a per-connection Python aggregate as on SQLite). This wrapper
    only covers connection establishment + the execute/row seam.
    """

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def execute(self, sql, params=None):
        import erpclaw_lib.authority_sink as _authority_sink
        _authority_sink.guard_statement(self, sql)
        _check_ledger_write(self, sql)
        cur = self._conn.cursor()
        if params is None:
            cur.execute(sql)
        else:
            cur.execute(_qmark_to_pyformat(sql), params)
        return cur

    def executemany(self, sql, seq_of_params):
        import erpclaw_lib.authority_sink as _authority_sink
        _authority_sink.guard_statement(self, sql)
        _check_ledger_write(self, sql)
        cur = self._conn.cursor()
        cur.executemany(_qmark_to_pyformat(sql), seq_of_params)
        return cur

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        try:
            setattr(self._conn, name, value)
        except (AttributeError, TypeError):
            object.__setattr__(self, name, value)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *args):
        import erpclaw_lib.authority_sink as _authority_sink
        return _authority_sink.exit_wrapper(self, args)

    def commit(self):
        import erpclaw_lib.authority_sink as _authority_sink
        return _authority_sink.commit_wrapper(self)

    def rollback(self):
        import erpclaw_lib.authority_sink as _authority_sink
        return _authority_sink.rollback_wrapper(self)

    def close(self):
        import erpclaw_lib.authority_sink as _authority_sink
        return _authority_sink.close_wrapper(self)


# Transaction-scoped advisory lock serialising first-time creation of the
# ``decimal_sum`` aggregate and its ``erpclaw_decimal_sum_sfunc`` /
# ``erpclaw_decimal_sum_ffunc`` support functions: concurrent first connects to
# a fresh schema must never fail with ``tuple concurrently updated``
# (concurrent ``CREATE OR REPLACE`` of the same function) or a duplicate-object
# error (both passing the existence check, both creating the aggregate). Taken
# with ``pg_advisory_xact_lock`` inside a single transaction, so the commit
# releases it.
_PG_DECIMAL_SUM_SETUP_LOCK = 4820471820369641307


def _ensure_pg_decimal_sum(conn) -> None:
    """Register the ``decimal_sum(text)`` aggregate on PostgreSQL if absent.

    On SQLite the aggregate is a per-connection Python registration
    (``conn.create_aggregate`` in :func:`get_connection`); PostgreSQL needs a
    persistent SQL aggregate object instead. This mirrors that registration so
    both backends expose ``decimal_sum(col)`` — financial sums over TEXT-stored
    Decimal amounts (cross-DB add-on C).

    The aggregate sums each value as ``numeric`` (exact, no float drift) and
    returns the total as TEXT, matching the SQLite ``_DecimalSum.finalize``
    contract so call sites can keep doing ``to_decimal(str(row["total"]))``.
    Fast path: one search-path-scoped existence read
    (``to_regprocedure('decimal_sum(text)')``); when the aggregate is already
    registered no DDL runs at all. Slow path: under a transaction-scoped
    advisory lock the existence read is repeated and only the winner creates
    the two support functions (``CREATE OR REPLACE``, same bodies) and the
    aggregate (same definition); the commit releases the lock. The DDL runs
    inside a savepoint so a loser whose re-check raced a winner's commit
    (``to_regprocedure`` can keep reporting a just-superseded answer inside
    the loser's transaction) turns its duplicate-object error into success
    instead of failing the connect; any other error still propagates.
    """
    import psycopg2
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT to_regprocedure('decimal_sum(text)') IS NOT NULL"
        )
        row = cur.fetchone()
        if row is not None and row[0]:
            conn.commit()
            return
        # End the fast-path read here (commit, never rollback: the
        # transaction also carries setup_pragmas' SETs, which a rollback
        # would undo). The slow path below must start a fresh transaction:
        # to_regprocedure() keeps reporting whatever was committed before
        # its transaction's first catalog read, so re-checking in this same
        # transaction would repeat the fast path's answer and miss a winner
        # that committed while waiting on the lock.
        conn.commit()
        cur.execute(
            "SELECT pg_advisory_xact_lock(%d)" % _PG_DECIMAL_SUM_SETUP_LOCK
        )
        cur.execute(
            "SELECT to_regprocedure('decimal_sum(text)') IS NOT NULL"
        )
        row = cur.fetchone()
        if row is not None and row[0]:
            conn.commit()
            return
        cur.execute("SAVEPOINT erpclaw_decimal_sum_setup")
        try:
            cur.execute(
                """
                CREATE OR REPLACE FUNCTION erpclaw_decimal_sum_sfunc(numeric, text)
                RETURNS numeric LANGUAGE sql IMMUTABLE AS
                $$ SELECT $1 + COALESCE($2::numeric, 0) $$;
                """
            )
            cur.execute(
                """
                CREATE OR REPLACE FUNCTION erpclaw_decimal_sum_ffunc(numeric)
                RETURNS text LANGUAGE sql IMMUTABLE AS
                $$ SELECT $1::text $$;
                """
            )
            cur.execute(
                """
                CREATE AGGREGATE decimal_sum(text) (
                  sfunc = erpclaw_decimal_sum_sfunc,
                  stype = numeric,
                  finalfunc = erpclaw_decimal_sum_ffunc,
                  initcond = '0'
                );
                """
            )
        except (psycopg2.errors.DuplicateObject,
                psycopg2.errors.DuplicateFunction):
            # A concurrent first connect created and committed the aggregate
            # after our re-check ran: the duplicate itself proves the desired
            # end state exists, so discard our redundant rewrites and report
            # success. Anything else (notably a concurrent catalog rewrite
            # surfacing as tuple-concurrently-updated) still propagates.
            cur.execute("ROLLBACK TO SAVEPOINT erpclaw_decimal_sum_setup")
        conn.commit()
    finally:
        cur.close()


def _resolve_pg_url(db_path=None) -> str:
    """Resolve the PostgreSQL connection URL for the active config.

    Precedence: the ``db_path`` argument, then ``ERPCLAW_DB_URL``, then ``ERPCLAW_DB_PATH``, so a single ``ERPCLAW_DB_PATH=postgresql://...`` works end-to-end. The migration runner puts ``ERPCLAW_DB_URL`` first and passes its resolved target explicitly. Raises if none is set.
    """
    url = db_path or os.environ.get("ERPCLAW_DB_URL") or os.environ.get("ERPCLAW_DB_PATH")
    if not url:
        raise RuntimeError(
            "ERPCLAW_DB_DIALECT=postgresql but no connection URL "
            "(set ERPCLAW_DB_URL or pass db_path)."
        )
    return url


def require_pg_url(target, *, source):
    """Refuse a PostgreSQL target that is not a URL, before any connection."""
    if isinstance(target, str) and (target.startswith("postgresql://")
                                     or target.startswith("postgres://")):
        return target
    raise RuntimeError(
        "PostgreSQL target from %s is not a postgresql:// URL; use the URL form,"
        " for example postgresql:///<database>?host=<socket directory>."
        " A libpq keyword string (dbname=... host=...) is not accepted here." % (source,)
    )


def _pg_connect_with_retry(psycopg2, url, *, cursor_factory, attempts=4):
    """Open a psycopg2 connection, retrying transient connection failures.

    A real Postgres deployment reaches the server over a network (or, in the
    test harness, an SSH tunnel). A momentary blip — the tunnel re-establishing,
    a transient DNS/TCP hiccup, the server reloading — surfaces as
    ``OperationalError`` at connect time. A single attempt turns that blip into
    a hard failure; a few bounded retries with backoff ride it out. SQLite is a
    local file and never takes this path.

    Only connection-level ``OperationalError`` is retried. Auth failures,
    missing-database, and every other error re-raise immediately so a real
    misconfiguration still fails fast. The final attempt's exception propagates
    unchanged so callers see the true cause, not a swallowed one.
    """
    delay = 0.5
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return psycopg2.connect(url, cursor_factory=cursor_factory)
        except psycopg2.OperationalError as exc:
            msg = str(exc).lower()
            # Retry only transient connection-establishment failures, not auth
            # ("password authentication failed") or missing-db ("does not exist").
            transient = (
                "could not connect" in msg
                or "connection refused" in msg
                or "connection reset" in msg
                or "timeout expired" in msg
                or "server closed the connection" in msg
                or "could not translate host" in msg
                or "no route to host" in msg
                or "temporarily unavailable" in msg
                or "the database system is starting up" in msg
            )
            if not transient or attempt == attempts:
                raise
            last_exc = exc
            time.sleep(delay)
            delay = min(delay * 2, 4.0)
    # Unreachable: the loop either returns or raises. Guard for clarity.
    raise last_exc  # pragma: no cover


def get_connection(db_path=None):
    """Get a database connection with ERPClaw standard settings, dialect-aware.

    SQLite (default) applies:
      - PRAGMA journal_mode=WAL  (concurrent reads during writes)
      - PRAGMA foreign_keys=ON   (enforce FK constraints)
      - PRAGMA busy_timeout=5000 (wait 5s on lock contention)
      - the ``decimal_sum`` aggregate, ``sqlite3.Row`` row access, and a
        0600 permission bit on freshly-created DB files.

    PostgreSQL (``ERPCLAW_DB_DIALECT=postgresql``) returns a
    :class:`PgConnectionWrapper` over a psycopg2 connection with a
    ``DictCursor`` and ``lock_timeout`` / ``statement_timeout`` set. The
    wrapper exposes the same ``conn.execute(sql, params)`` API, translating
    ``?`` placeholders to ``%s``.

    Args:
        db_path: SQLite file path, or a Postgres URL when the dialect is
                 postgresql. Defaults to ~/.openclaw/erpclaw/data.sqlite
                 (SQLite) or ERPCLAW_DB_URL / ERPCLAW_DB_PATH (Postgres).
                 Also checks the ERPCLAW_DB_PATH environment variable.

    Returns:
        ConnectionWrapper (SQLite) or PgConnectionWrapper (PostgreSQL).

    When read-only mode is requested, SQLite refuses with FileNotFoundError
    unless the file already exists and never creates directories or changes
    permissions. PostgreSQL connections skip the server-side aggregate
    registration and are handed back idle in read-only mode.
    """
    if get_dialect() == "postgresql":
        import psycopg2
        from psycopg2.extras import DictCursor
        url = _resolve_pg_url(db_path)
        read_only = readonly_requested()
        conn = _pg_connect_with_retry(psycopg2, url, cursor_factory=DictCursor)
        setup_pragmas(conn)
        # Mirror the SQLite create_aggregate registration: ensure the
        # decimal_sum() SQL aggregate exists for exact financial sums.
        if not read_only:
            _ensure_pg_decimal_sum(conn)
        # SET lock_timeout/statement_timeout opened an implicit transaction;
        # commit so the connection is handed back idle, not in-transaction.
        conn.commit()
        return PgConnectionWrapper(conn)

    path = db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)
    read_only = readonly_requested()
    if read_only:
        _reject_url_shaped_path(path)
        if not os.path.exists(path):
            raise FileNotFoundError(
                "database file does not exist and ERPCLAW_DB_READONLY=1 "
                "refuses to create it: %s" % path
            )
        is_new = False
        # Read-only storage opens through a mode=ro URI, so the driver itself
        # refuses every write on this handle; query_only below stays as the
        # second, connection-local guard.
        target, as_uri = readonly_sqlite_uri(path), True
    else:
        ensure_db_exists(path)
        is_new = not os.path.exists(path)
        target, as_uri = path, False
    conn = sqlite3.connect(target, uri=as_uri)
    conn.row_factory = sqlite3.Row
    if not read_only:
        _enable_wal(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.create_aggregate("decimal_sum", 1, _DecimalSum)
    if read_only:
        _apply_readonly(conn)
    if is_new:
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        except OSError:
            pass  # non-fatal on some platforms
    return ConnectionWrapper(conn)


def get_readonly_connection(db_path):
    """Open an existing SQLite file read-only for observation.

    The path argument is required and must name an existing regular file.
    Nothing is created: no directory is made, no permission bit is touched,
    and no environment variable or compiled-in default location is consulted.
    Empty values, in-memory database names, directory paths, and URL- or
    URI-shaped values are refused before any driver call, without echoing
    the supplied value when it could carry credentials. A PostgreSQL dialect
    selection is refused before its driver is imported; this entry point is
    SQLite-only by contract.

    The file is opened through a read-only URI with the filesystem path
    percent-encoded, so spaces, non-ASCII names, and reserved characters
    cannot alter the URI structure or inject extra URI parameters. The
    handle carries a connection-local query-only guard and a row factory
    matching the observation checks. No directory setup, permission change,
    journal-mode switch, checkpoint, commit, aggregate registration, engine
    use, or provisioning helper runs here; the regular write-path entry
    point is unchanged.

    This is a trusted-observer handle for first-party inspection helpers.
    It is not a sandbox for untrusted statements or untrusted code; do not
    hand it to an untrusted agent.

    SQLite may still need the database sidecar files to be present alongside
    the main file, or may place coordination sidecars on an ordinary writable
    directory while reading. Zero filesystem change is qualified only for a
    separately sealed, consistent fixture with usable existing sidecars.
    """
    # Dialect gate first: refuse before any driver import or open attempt.
    if get_dialect() == "postgresql":
        raise RuntimeError(
            "get_readonly_connection is SQLite-only: refusing a PostgreSQL "
            "dialect selection before any driver import or open attempt."
        )
    if get_dialect() != "sqlite":
        raise RuntimeError(
            "get_readonly_connection does not support this dialect."
        )
    if db_path is None:
        raise ValueError("get_readonly_connection requires a database path.")
    raw = db_path
    if isinstance(db_path, os.PathLike):
        raw = os.fspath(db_path)
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    if not isinstance(raw, str):
        raise TypeError("get_readonly_connection requires a path string.")
    if raw == "" or raw.strip() == "":
        raise ValueError("get_readonly_connection requires a database path.")
    path_value = raw
    if path_value.strip() == ":memory:":
        raise ValueError(
            "get_readonly_connection refuses an in-memory database name."
        )
    # URI-shaped values are refused without echoing the value itself, which
    # may carry credentials. The shared helper covers scheme URLs; the file
    # scheme needs its own check because it is not in the helper set.
    if ":" in path_value and path_value.split(":", 1)[0].lower() == "file":
        raise ValueError(
            "get_readonly_connection requires a filesystem path, not a URI."
        )
    _reject_url_shaped_path(path_value)
    if os.path.isdir(path_value):
        raise IsADirectoryError(
            "get_readonly_connection requires a regular file: %r is a "
            "directory." % (path_value,)
        )
    if not os.path.isfile(path_value):
        raise FileNotFoundError(
            "read-only database file does not exist: %s" % (path_value,)
        )
    from urllib.parse import quote as _quote
    absolute = os.path.abspath(path_value)
    # Owner read path: percent-encode the filesystem location so reserved
    # characters cannot reshape the URI, then open strictly read-only.
    uri = "file:" + _quote(absolute, safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        # Owner read guard: connection-local query-only defense.
        conn.execute("PRAGMA query_only=ON")
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise
    return ConnectionWrapper(conn)


def _reject_url_shaped_path(path: str) -> None:
    """Refuse a connection URL handed in where a filesystem path belongs.

    This is not hypothetical tidiness. `ensure_db_exists` used to run
    `os.makedirs` on whatever it was given, so a caller passing
    `postgresql://user:secret@host/db` created a DIRECTORY TREE NAMED AFTER THE
    CONNECTION STRING — credentials and all — and then SQLite happily made a
    database file inside it. Twice observed: a June-era tree on the test server
    whose directory name embedded the then-current PostgreSQL password, and a
    zero-byte `postgresql:/erpclaw@localhost/erpclaw_test` file that reached a
    commit on 2026-08-13.

    Writing a password into a filename is worse than the failure it replaces.
    A path that names a scheme is a caller mistake every time, so it fails
    loudly here rather than being silently turned into a directory.
    """
    if "://" in path or path.split(":", 1)[0] in _URL_SCHEMES:
        scheme = path.split(":", 1)[0]
        raise ValueError(
            "database path looks like a %s connection URL, not a filesystem "
            "path: pass it as ERPCLAW_DB_URL (or the module's --db-url) so it "
            "is parsed as a URL. Refusing rather than creating a directory "
            "named after it, because a URL can carry a password and a "
            "directory name is not a place for one." % scheme
        )


_URL_SCHEMES = frozenset({"postgresql", "postgres", "psql", "mysql", "sqlite"})


def ensure_db_exists(db_path=None) -> str:
    """Ensure the database directory exists.

    Creates parent directories if needed. Does not create the DB file
    itself — sqlite3.connect() handles that.

    Refuses a value that names a URL scheme; see `_reject_url_shaped_path`.

    Args:
        db_path: Path to the database file.

    Returns:
        The resolved database path.
    """
    path = db_path or DEFAULT_DB_PATH
    _reject_url_shaped_path(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return path
