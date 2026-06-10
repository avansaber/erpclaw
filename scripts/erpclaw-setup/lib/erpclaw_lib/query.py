"""
ERPClaw Query Builder — convenience layer on top of vendored PyPika.

Usage:
    from erpclaw_lib.query import Q, Table, Field, Case, fn, P
    from erpclaw_lib.query import DecimalSum, DecimalAbs

    # Build a parameterized query
    t = Table('company')
    q = Q.from_(t).select(t.star).where(t.id == P())
    sql = q.get_sql()       # SELECT * FROM "company" WHERE "id"=?
    params = ['company-id']  # You still manage params separately

    # Financial queries use DecimalSum (not SUM) for Decimal precision
    gl = Table('gl_entry')
    q = Q.from_(gl).select(gl.account, DecimalSum(gl.debit).as_('total_debit'))

Notes:
    - Q = SQLLiteQuery (SQLite dialect, our default)
    - P = QmarkParameter (returns ? placeholder)
    - fn = PyPika functions module (fn.Sum, fn.Count, fn.Coalesce, etc.)
    - DecimalSum wraps our custom decimal_sum() SQLite aggregate
    - DecimalAbs wraps our custom decimal_abs() SQLite aggregate
    - All generated SQL uses double-quoted identifiers (PyPika default)
    - PyPika does NOT manage parameter values — you pass params separately
      to conn.execute(sql, params)
"""

from erpclaw_lib.vendor.pypika import (
    SQLLiteQuery,
    Query,
    Table,
    Field,
    Case,
    Order,
    Criterion,
    CustomFunction,
    Not,
    NullValue,
    QmarkParameter,
)
from erpclaw_lib.vendor.pypika import fn
from erpclaw_lib.vendor.pypika.terms import Function, LiteralValue, ValueWrapper, Star


# ── Aliases for brevity ──

Q = SQLLiteQuery
"""Default query builder — SQLite dialect."""

P = QmarkParameter
"""Parameterized placeholder — returns ? for SQLite."""

NULL = NullValue()


# ── Custom aggregate functions (registered in erpclaw_lib/db.py) ──

class DecimalSum(Function):
    """Wrapper for ERPClaw's custom decimal_sum() SQLite aggregate.

    Uses Python Decimal internally for exact financial arithmetic.
    Registered via db.get_connection() → conn.create_aggregate('decimal_sum', ...).
    """
    def __init__(self, term, alias=None):
        super().__init__("decimal_sum", term, alias=alias)


class DecimalAbs(Function):
    """Wrapper for ERPClaw's custom decimal_abs() SQLite function."""
    def __init__(self, term, alias=None):
        super().__init__("decimal_abs", term, alias=alias)


# ── Helper: build WHERE clause from dict ──

def where_eq(query, table, filters):
    """Apply equality filters from a dict.

    Usage:
        q = Q.from_(t).select(t.star)
        q = where_eq(q, t, {'company_id': P(), 'status': 'Active'})
        # → WHERE "company_id"=? AND "status"='Active'
    """
    for col, val in filters.items():
        q = query.where(Field(col) == val)
        query = q
    return query


# ── Helper: build INSERT with named columns ──

def insert_row(table_name, data):
    """Build INSERT INTO table (col1, col2, ...) VALUES (?, ?, ...).

    Args:
        table_name: str — table name
        data: dict — column: value mapping (values should be P() for params)

    Returns:
        tuple: (sql_string, column_names_in_order)

    Usage:
        sql, cols = insert_row('company', {
            'id': P(), 'name': P(), 'status': P()
        })
        conn.execute(sql, [uuid, name, status])
    """
    t = Table(table_name)
    q = Q.into(t).columns(*data.keys()).insert(*data.values())
    return q.get_sql(), list(data.keys())


# ── Helper: build UPDATE with named columns ──

def update_row(table_name, data, where):
    """Build UPDATE table SET col1=?, col2=? WHERE id=?.

    Args:
        table_name: str
        data: dict — column: value pairs to SET
        where: dict — column: value pairs for WHERE clause

    Returns:
        sql_string

    Usage:
        sql = update_row('company',
            data={'name': P(), 'updated_at': P()},
            where={'id': P()})
        conn.execute(sql, [new_name, now, company_id])
    """
    t = Table(table_name)
    q = Q.update(t)
    for col, val in data.items():
        q = q.set(Field(col), val)
    for col, val in where.items():
        q = q.where(Field(col) == val)
    return q.get_sql()


# ── Helper: build dynamic UPDATE (only SET columns that are provided) ──

def dynamic_update(table_name, data, where):
    """Build UPDATE with dynamic SET columns, returning (sql, params).

    Unlike update_row() which requires pre-placed P() markers and returns
    only the SQL string, dynamic_update() accepts real values, separates
    them into parameter placeholders, and returns both the SQL and the
    ordered parameter list ready for conn.execute().

    LiteralValue entries in *data* or *where* are rendered inline (no
    placeholder) — use this for SQL expressions like now() or today().

    Args:
        table_name: str — target table
        data: dict — {column: value} pairs to SET.  Values may be plain
              Python objects (parameterized) or LiteralValue instances
              (rendered inline).
        where: dict — {column: value} pairs for the WHERE clause.  Same
               LiteralValue support as *data*.

    Returns:
        tuple: (sql_string, params_list)

    Usage:
        from erpclaw_lib.query import dynamic_update, now

        data = {
            "name": "New Name",
            "status": "active",
            "updated_at": now(),
        }
        where = {"id": entity_id}
        sql, params = dynamic_update("my_table", data, where)
        conn.execute(sql, params)
    """
    t = Table(table_name)
    q = Q.update(t)
    params = []

    for col, val in data.items():
        if isinstance(val, LiteralValue):
            q = q.set(Field(col), val)
        else:
            q = q.set(Field(col), P())
            params.append(val)

    for col, val in where.items():
        if isinstance(val, LiteralValue):
            q = q.where(Field(col) == val)
        else:
            q = q.where(Field(col) == P())
            params.append(val)

    return q.get_sql(), params


# ── Re-exports for clean imports ──

__all__ = [
    'Q', 'P', 'Query', 'Table', 'Field', 'Case', 'Order',
    'Criterion', 'CustomFunction', 'Not', 'NULL',
    'fn', 'Star', 'ValueWrapper', 'LiteralValue',
    'DecimalSum', 'DecimalAbs',
    'where_eq', 'insert_row', 'update_row', 'dynamic_update',
    'SQLLiteQuery', 'QmarkParameter',
    'now', 'today', 'date_format', 'coalesce', 'ilike',
    'json_get', 'string_agg', 'days_between', 'hours_between',
    'seconds_between', 'abs_days_between',
    'ddl_now', 'ddl_today',
    'line_order', 'rowid_col', 'latest_insert_order', 'scalar_max',
]


# ── Dialect detection ──
import os as _os
_DIALECT = _os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


# ── Dialect-aware SQL helpers ──
# Domain code should use THESE instead of LiteralValue() with DB-specific functions.
# These are the ONLY place in the codebase that knows which database is running.

def now():
    """Current timestamp as TEXT — dialect-aware.

    Replaces: LiteralValue("datetime('now')")
    """
    if _DIALECT == "postgresql":
        return LiteralValue("NOW()::text")
    if _DIALECT == "mysql":
        return LiteralValue("NOW()")
    return LiteralValue("datetime('now')")


def today():
    """Current date as TEXT — dialect-aware.

    Replaces: LiteralValue("date('now')")
    """
    if _DIALECT == "postgresql":
        return LiteralValue("CURRENT_DATE::text")
    if _DIALECT == "mysql":
        return LiteralValue("CURDATE()")
    return LiteralValue("date('now')")


def date_format(col, fmt):
    """SQL-level date formatting — dialect-aware.

    Uses Python-style format codes: %Y, %m, %d, %H, %M, %S.
    Replaces: LiteralValue("strftime('%Y-%m', col)")
    """
    if _DIALECT == "postgresql":
        pg_fmt = fmt.replace('%Y', 'YYYY').replace('%m', 'MM').replace('%d', 'DD')
        pg_fmt = pg_fmt.replace('%H', 'HH24').replace('%M', 'MI').replace('%S', 'SS')
        return LiteralValue(f"to_char({col}, '{pg_fmt}')")
    if _DIALECT == "mysql":
        return LiteralValue(f"DATE_FORMAT({col}, '{fmt}')")
    return LiteralValue(f"strftime('{fmt}', {col})")


def coalesce(*args):
    """Null coalescing — ANSI SQL, works on ALL databases.

    Replaces: IFNULL(col, default) which is SQLite-only.
    COALESCE is universal — SQLite, PostgreSQL, MySQL, Oracle all support it.
    """
    args_str = ", ".join(str(a) for a in args)
    return LiteralValue(f"COALESCE({args_str})")


def ilike(field_expr, pattern):
    """Case-insensitive LIKE — portable across all databases.

    Uses LOWER() on both sides for consistent case-insensitive matching
    on SQLite, PostgreSQL, and MySQL.
    """
    return LiteralValue(f"LOWER({field_expr}) LIKE LOWER({pattern})")


def _sql_str_literal(s):
    """Render ``s`` as a safe single-quoted SQL string literal.

    Doubles embedded single quotes (the ANSI escape, valid on SQLite,
    PostgreSQL, and MySQL). The key is interpolated into the SQL TEXT of the
    helper (not a bound parameter, since it names a JSON path / object key),
    so it MUST be escaped here — a Python ``repr`` would flip to double quotes
    on an embedded ``'`` and produce a broken / injectable identifier on
    Postgres. Keys are normally drawn from the dimension_registry, but the
    helper does not rely on that.
    """
    return "'" + str(s).replace("'", "''") + "'"


def json_get(col, key):
    """JSON field access — dialect-aware.

    Replaces: LiteralValue("json_extract(col, '$.key')")

    Postgres note: ``dimensions_json`` (and peer JSON columns) are declared
    ``TEXT`` in the schema, and Postgres provisions them as ``text`` — the
    ``->>`` operator does NOT exist for ``text``, so the column is cast to
    ``jsonb`` first. The key is a plain object key for ``->>`` (NOT a SQLite
    ``$.key`` JSONPath). Both verified on PostgreSQL 16 (Wave 1 P0 / SIM-0).
    """
    if _DIALECT == "postgresql":
        return LiteralValue(f"{col}::jsonb->>{_sql_str_literal(key)}")
    if _DIALECT == "mysql":
        return LiteralValue(
            f"JSON_UNQUOTE(JSON_EXTRACT({col}, {_sql_str_literal('$.' + str(key))}))"
        )
    return LiteralValue(
        f"json_extract({col}, {_sql_str_literal('$.' + str(key))})"
    )


def string_agg(col, separator="', '"):
    """String aggregation — dialect-aware.

    Replaces: LiteralValue("GROUP_CONCAT(col, sep)")
    """
    if _DIALECT == "postgresql":
        return LiteralValue(f"STRING_AGG({col}, {separator})")
    if _DIALECT == "mysql":
        return LiteralValue(f"GROUP_CONCAT({col} SEPARATOR {separator})")
    return LiteralValue(f"GROUP_CONCAT({col}, {separator})")


def days_between(d1, d2):
    """Date difference in days — dialect-aware.

    Replaces: LiteralValue("julianday(d1) - julianday(d2)")
    """
    if _DIALECT == "postgresql":
        return LiteralValue(f"EXTRACT(DAY FROM ({d1}::timestamp - {d2}::timestamp))")
    if _DIALECT == "mysql":
        return LiteralValue(f"DATEDIFF({d1}, {d2})")
    return LiteralValue(f"julianday({d1}) - julianday({d2})")


def hours_between(t1, t2):
    """Time difference in hours — dialect-aware.

    Replaces: LiteralValue("(julianday(t1) - julianday(t2)) * 24")
    """
    if _DIALECT == "postgresql":
        return LiteralValue(f"EXTRACT(EPOCH FROM ({t1}::timestamp - {t2}::timestamp)) / 3600")
    if _DIALECT == "mysql":
        return LiteralValue(f"TIMESTAMPDIFF(HOUR, {t2}, {t1})")
    return LiteralValue(f"(julianday({t1}) - julianday({t2})) * 24")


def seconds_between(t1, t2):
    """Time difference in seconds — dialect-aware.

    Replaces: LiteralValue("(julianday(t1) - julianday(t2)) * 86400")
    """
    if _DIALECT == "postgresql":
        return LiteralValue(f"EXTRACT(EPOCH FROM ({t1}::timestamp - {t2}::timestamp))")
    if _DIALECT == "mysql":
        return LiteralValue(f"TIMESTAMPDIFF(SECOND, {t2}, {t1})")
    return LiteralValue(f"(julianday({t1}) - julianday({t2})) * 86400")


def abs_days_between(d1, d2):
    """Absolute date difference in days — dialect-aware.

    Replaces: ABS(julianday(d1) - julianday(d2))
    """
    if _DIALECT == "postgresql":
        return LiteralValue(f"ABS(EXTRACT(DAY FROM ({d1}::timestamp - {d2}::timestamp)))")
    if _DIALECT == "mysql":
        return LiteralValue(f"ABS(DATEDIFF({d1}, {d2}))")
    return LiteralValue(f"ABS(julianday({d1}) - julianday({d2}))")


def line_order(table=None):
    """ORDER BY field for stable document line-item display — dialect-aware.

    Line-item tables (sales_order_item, quotation_item, purchase_order_item, ...)
    carry no explicit order column, only a UUID ``id``.  SQLite has historically
    ordered them by the implicit ``rowid`` (true insertion order).  PostgreSQL has
    no ``rowid`` (``ORDER BY rowid`` → 42703 column-does-not-exist), so it falls
    back to ``id``: deterministic, but arbitrary line order.  See ERP-33 (the
    Option-A dialect seam; SQLite behavior is unchanged).

    Pass a PyPika table/alias to scope the column (``"qi"."rowid"``); omit it for a
    bare column on a single-table query.
    """
    col = "id" if _DIALECT == "postgresql" else "rowid"
    return table.field(col) if table is not None else Field(col)


def rowid_col(alias=""):
    """Raw-SQL column standing in for SQLite's ``rowid`` — dialect-aware.

    SQLite: ``<alias>rowid``.  PostgreSQL: ``<alias>id``.  Use only as a *tiebreak*
    after a meaningful order key (e.g. ``created_at``); ``id`` alone is arbitrary,
    not insertion order.  ``alias`` includes the trailing dot, e.g. ``"ge."``.

    Keeping the same seam on both the chain-build (gl_posting) and chain-verify
    (erpclaw-gl) sides keeps the GL hash chain self-consistent per backend.
    """
    return f"{alias}id" if _DIALECT == "postgresql" else f"{alias}rowid"


def insert_or_ignore(sql):
    """Portable "insert, ignore on duplicate" — dialect-aware.

    SQLite spells it ``INSERT OR IGNORE ...``; PostgreSQL has no such verb and
    uses ``INSERT ... ON CONFLICT DO NOTHING`` (target-less = ignore on ANY
    unique/PK conflict, the OR-IGNORE equivalent). Pass the full SQLite-form
    INSERT string; on Postgres the ``OR IGNORE`` is dropped and the conflict
    clause appended. Values still use ``?`` placeholders as usual.
    """
    if _DIALECT == "postgresql":
        return sql.replace("INSERT OR IGNORE", "INSERT", 1).rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return sql


def latest_insert_order(alias=""):
    """Raw-SQL ORDER BY body selecting the most-recently-inserted row — dialect-aware.

    SQLite: ``<alias>rowid DESC`` (insertion order, unchanged).  PostgreSQL:
    ``<alias>created_at DESC, <alias>id DESC`` — ``created_at`` mirrors insertion
    time and ``id`` is the deterministic tiebreak, together reproducing "latest
    inserted".  Plain ``id DESC`` would pick an arbitrary row, so it is NOT used.
    ``alias`` includes the trailing dot, e.g. ``"s2."``.  Every table ordered this
    way (stock_ledger_entry) carries ``created_at``.
    """
    if _DIALECT == "postgresql":
        return f"{alias}created_at DESC, {alias}id DESC"
    return f"{alias}rowid DESC"


def scalar_max(*exprs):
    """Raw-SQL scalar (row-wise) maximum over the given expressions — dialect-aware.

    SQLite's ``MAX(a, b, ...)`` with 2+ arguments is a *scalar* function (the
    greatest of its arguments).  PostgreSQL reserves ``MAX`` for the aggregate
    and spells the scalar form ``GREATEST(a, b, ...)``.  Pass already-formed SQL
    expression strings (commonly already wrapped in ``CAST(... AS NUMERIC)``);
    returns the dialect-appropriate fragment, e.g.
    ``scalar_max("CAST(x AS NUMERIC)", "0")`` → ``GREATEST(...)`` on PG,
    ``MAX(...)`` on SQLite.
    """
    body = ", ".join(exprs)
    fname = "GREATEST" if _DIALECT == "postgresql" else "MAX"
    return f"{fname}({body})"


def ddl_now():
    """DDL DEFAULT expression for current timestamp — dialect-aware.

    Used in CREATE TABLE: DEFAULT (ddl_now())
    NOT used in queries — use now() for queries.
    """
    if _DIALECT == "postgresql":
        return "NOW()"
    if _DIALECT == "mysql":
        return "NOW()"
    return "datetime('now')"


def ddl_today():
    """DDL DEFAULT expression for current date — dialect-aware.

    Used in CREATE TABLE: DEFAULT (ddl_today())
    """
    if _DIALECT == "postgresql":
        return "CURRENT_DATE"
    if _DIALECT == "mysql":
        return "CURDATE()"
    return "date('now')"
