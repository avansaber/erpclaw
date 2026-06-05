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
The Postgres connection URL is resolved from the ``db_path`` argument,
``ERPCLAW_DB_URL``, or ``ERPCLAW_DB_PATH`` (mirrors the migration runner).
"""
import os
import sqlite3
import stat
import time
from decimal import Decimal


DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/erpclaw/data.sqlite")


def get_dialect():
    """Return the configured database dialect."""
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


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
    For MySQL: no equivalent needed (InnoDB handles these).
    """
    dialect = get_dialect()
    if dialect == "sqlite":
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
    elif dialect == "postgresql":
        lock_timeout = os.environ.get("ERPCLAW_PG_LOCK_TIMEOUT", "5s")
        statement_timeout = os.environ.get("ERPCLAW_PG_STATEMENT_TIMEOUT", "0")
        cur = conn.cursor() if hasattr(conn, 'cursor') else conn
        try:
            cur.execute("SET lock_timeout = %s", (lock_timeout,))
            cur.execute("SET statement_timeout = %s", (statement_timeout,))
        finally:
            if cur is not conn:
                cur.close()


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
        return self._conn.__exit__(*args)

    def __call__(self, *args, **kwargs):
        return self._conn(*args, **kwargs)


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
        cur = self._conn.cursor()
        if params is None:
            cur.execute(sql)
        else:
            cur.execute(_qmark_to_pyformat(sql), params)
        return cur

    def executemany(self, sql, seq_of_params):
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
        return self._conn.__exit__(*args)


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
    Idempotent and race-tolerant: the support functions use
    ``CREATE OR REPLACE`` and the aggregate is guarded by an existence check.
    """
    cur = conn.cursor()
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
            DO $do$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM pg_proc
                WHERE proname = 'decimal_sum' AND prokind = 'a'
              ) THEN
                CREATE AGGREGATE decimal_sum(text) (
                  sfunc = erpclaw_decimal_sum_sfunc,
                  stype = numeric,
                  finalfunc = erpclaw_decimal_sum_ffunc,
                  initcond = '0'
                );
              END IF;
            END
            $do$;
            """
        )
        conn.commit()
    finally:
        cur.close()


def _resolve_pg_url(db_path=None) -> str:
    """Resolve the PostgreSQL connection URL for the active config.

    Precedence mirrors the migration runner (``db_path or ERPCLAW_DB_URL``),
    with ``ERPCLAW_DB_PATH`` accepted as a final fallback so a single
    ``ERPCLAW_DB_PATH=postgresql://...`` works end-to-end. Raises if none is set.
    """
    url = db_path or os.environ.get("ERPCLAW_DB_URL") or os.environ.get("ERPCLAW_DB_PATH")
    if not url:
        raise RuntimeError(
            "ERPCLAW_DB_DIALECT=postgresql but no connection URL "
            "(set ERPCLAW_DB_URL or pass db_path)."
        )
    return url


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
    """
    if get_dialect() == "postgresql":
        import psycopg2
        from psycopg2.extras import DictCursor
        url = _resolve_pg_url(db_path)
        conn = _pg_connect_with_retry(psycopg2, url, cursor_factory=DictCursor)
        setup_pragmas(conn)
        # Mirror the SQLite create_aggregate registration: ensure the
        # decimal_sum() SQL aggregate exists for exact financial sums.
        _ensure_pg_decimal_sum(conn)
        # SET lock_timeout/statement_timeout opened an implicit transaction;
        # commit so the connection is handed back idle, not in-transaction.
        conn.commit()
        return PgConnectionWrapper(conn)

    path = db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)
    ensure_db_exists(path)
    is_new = not os.path.exists(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.create_aggregate("decimal_sum", 1, _DecimalSum)
    if is_new:
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        except OSError:
            pass  # non-fatal on some platforms
    return ConnectionWrapper(conn)


def ensure_db_exists(db_path=None) -> str:
    """Ensure the database directory exists.

    Creates parent directories if needed. Does not create the DB file
    itself — sqlite3.connect() handles that.

    Args:
        db_path: Path to the database file.

    Returns:
        The resolved database path.
    """
    path = db_path or DEFAULT_DB_PATH
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return path
