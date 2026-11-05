"""Database setup: one SQLAlchemy metadata, on PostgreSQL.

Money is stored as TEXT holding an exact Decimal.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import (CheckConstraint, Column, Integer, MetaData, String, Table, create_engine, event, inspect,
                        select, text)
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.engine import Engine
from sqlalchemy.types import TypeDecorator

from .money import MINOR_UNITS, PLAIN, MoneyError, to_decimal

metadata = MetaData()


class Money(TypeDecorator):
    """Exact Decimal stored as text. Refuses floats on the way in."""

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return format(to_decimal(value), "f")

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return Decimal(value)


Qty = Money  # quantities are exact decimals too, with their own precision


class MoneyText(TypeDecorator):
    """A posted amount: binds only a Decimal in canonical spelling;
    the column's CHECK enforces the currency's exact scale in the database."""

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, Decimal):
            raise MoneyError("bind a Decimal")
        text_value = format(value, "f")
        if not PLAIN.match(text_value) or (text_value.startswith("-") and value == 0):
            raise MoneyError(f"not canonical: {text_value!r}")
        return text_value

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


def scale_check(column: str, currency_column: str) -> str:
    """CHECK: the stored text is canonical and has exactly the currency's scale
    for the currencies in MINOR_UNITS; others are refused."""
    a = column
    pos = f"strpos({a}, '.')"
    shape = f"{a} ~ '^-?(0|[1-9][0-9]*)(\\.[0-9]+)?$' AND {a} !~ '^-0(\\.0+)?$'"
    cases = " ".join(
        f"WHEN '{cur}' THEN {pos} = 0" if exp == 0 else f"WHEN '{cur}' THEN {pos} > 0 AND {pos} = length({a}) - {exp}"
        for cur, exp in sorted(MINOR_UNITS.items()))
    return f"({shape}) AND (CASE {currency_column} {cases} ELSE false END)"


def rate_check(column: str) -> str:
    """CHECK for a rate or quantity: canonical decimal text, positive, at most four decimal places
    (the contract input already bounds them so)."""
    a = column
    return f"{a} ~ '^(0|[1-9][0-9]*)(\\.[0-9]{{1,4}})?$' AND {a} ~ '[1-9]'"


def sign_check(column: str, sign: str) -> str:
    """CHECK on an amount's sign, on its canonical text: positive, not_negative or zero."""
    a = column
    neg, nonzero = f"{a} ~ '^-'", f"{a} ~ '[1-9]'"
    return {"positive": f"NOT ({neg}) AND {nonzero}", "not_negative": f"NOT ({neg})", "zero": f"NOT ({nonzero})"}[sign]


class decimal_sum(FunctionElement):
    """Exact SUM over money TEXT, as TEXT.
    No rows gives NULL ("no data"), never zero."""

    type = String()
    inherit_cache = True
    name = "decimal_sum"


@compiles(decimal_sum, "postgresql")
def _pg_sum(element, compiler, **kw):
    return f"CAST(SUM(CAST({compiler.process(element.clauses, **kw)} AS NUMERIC)) AS TEXT)"


def data_root() -> Path:
    """Where local mail, the assistant's own store and eval files live: $ERPCLAW_HOME, else ./var."""
    home = os.environ.get("ERPCLAW_HOME")
    return Path(home) if home else Path.cwd() / "var"


class RoleRefused(RuntimeError):
    """The database role or URL is not fit for the command."""


NEEDS_POSTGRESQL = "ERPClaw v3 needs PostgreSQL; see the install notes."


class NotPostgreSQL(RoleRefused):
    """The books URL does not name a PostgreSQL database."""


def require_postgresql(url: str | None) -> str:
    """The URL, when it names a PostgreSQL database; otherwise a refusal with the plain message."""
    from sqlalchemy.engine import make_url

    if not url:
        raise NotPostgreSQL(f"{NEEDS_POSTGRESQL} Set ERPCLAW_DATABASE_URL to the books' postgresql:// URL.")
    try:
        driver = make_url(url).drivername
    except ArgumentError:
        raise NotPostgreSQL(NEEDS_POSTGRESQL) from None
    if driver != "postgresql" and not driver.startswith("postgresql+"):
        raise NotPostgreSQL(NEEDS_POSTGRESQL)
    return url


def database_url() -> str:
    """The runtime's URL: the runtime role's login, from ERPCLAW_DATABASE_URL."""
    return require_postgresql(os.environ.get("ERPCLAW_DATABASE_URL"))


def migration_url() -> str:
    """The owner's URL, for the commands that create tables: ERPCLAW_MIGRATION_URL,
    which must name the runtime URL's database with a different role."""
    from sqlalchemy.engine import make_url

    mig = os.environ.get("ERPCLAW_MIGRATION_URL")
    url = database_url()
    if not mig:
        raise RoleRefused("This command runs as the owner role: set ERPCLAW_MIGRATION_URL.")
    a, b = make_url(url), make_url(mig)
    if (a.host, a.port, a.database) != (b.host, b.port, b.database) or not b.drivername.startswith("postgresql"):
        raise RoleRefused("ERPCLAW_MIGRATION_URL and ERPCLAW_DATABASE_URL must name the same database.")
    if a.username == b.username:
        raise RoleRefused("ERPCLAW_MIGRATION_URL and ERPCLAW_DATABASE_URL must use different roles.")
    return mig


def runtime_role() -> str:
    """The runtime role's name: the user of ERPCLAW_DATABASE_URL."""
    from sqlalchemy.engine import make_url

    role = make_url(database_url()).username
    if not role:
        raise RoleRefused("ERPCLAW_DATABASE_URL must name the runtime role.")
    return role


@dataclass(frozen=True)
class Runtime:
    """The PostgreSQL runtime role's privileges on one table: SELECT, INSERT, UPDATE on the named columns only, DELETE."""

    insert: bool = False
    update: tuple[str, ...] = ()
    delete: bool = False
    # columns granted UPDATE only so the role can lock rows (PostgreSQL needs one for FOR SHARE); the runtime
    # code never writes them (tests/test_runtime_role.py checks both)
    lock: tuple[str, ...] = ()


READ = Runtime()
APPEND = Runtime(insert=True)


OWNER_TABLE = "erpclaw_books"
SETUP_LOCK_KEY = 7_303_615_201  # PostgreSQL advisory lock: one initialization at a time
OWNER_MARK = "erpclaw-engine-books-v1"
DEMO_MARK = "erpclaw-demo-books"  # set only by the demo seed
EVAL_MARK = "erpclaw-eval-books"  # set only by the eval runner's book setup

owner = Table(OWNER_TABLE, metadata, Column("mark", String(64), primary_key=True),
              info={"owner": "core", "runtime": READ})

IDENTITY_TABLE = "books_identity"
UUID_SHAPE = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

# The books' identity: one row, written once when the books are initialised (or by the revision that added it,
# for books made before it). A trigger refuses every UPDATE, DELETE and TRUNCATE, so it never changes; a backup
# carries it and a restore copies it like every other row, so restored books keep the identity of their source.
books_identity = Table(IDENTITY_TABLE, metadata,
                       Column("id", Integer, primary_key=True, autoincrement=False),
                       Column("books_id", String(36), nullable=False),
                       CheckConstraint("id = 1", name="ck_books_identity_one_row"),
                       CheckConstraint(f"books_id ~ '{UUID_SHAPE}'", name="ck_books_identity_uuid"),
                       info={"owner": "core", "runtime": READ})


class ForeignBooks(RuntimeError):
    """The database holds tables but no ownership mark from this engine."""


def refuse_foreign_books(engine: Engine) -> None:
    """Never open books this engine does not own: a database that already has
    tables must carry this engine's ownership mark. An empty database is fine
    (it becomes ours on create_all). This check does not depend on where the
    books live, so it also protects books nobody listed. A read, on a READ ONLY
    transaction."""
    with read_only(engine) as conn:
        names = set(inspect(conn).get_table_names())
        if not names:
            return
        if OWNER_TABLE in names and conn.execute(select(owner.c.mark).where(owner.c.mark == OWNER_MARK)).first():
            return
    engine.dispose()
    raise ForeignBooks(f"refusing to open {engine.url.render_as_string(hide_password=True)}: "
                       "it holds tables but no ownership mark from this engine")


READ_ONLY = "erpclaw_read_only"


def read_only(engine: Engine):
    """A connection for reading only: its transactions are READ ONLY."""
    return engine.connect().execution_options(**{READ_ONLY: True})


def _postgres_begin(conn) -> None:
    if conn.get_execution_options().get(READ_ONLY):
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")


def make_engine(url: str | None = None) -> Engine:
    """An engine on the books at `url` (default: ERPCLAW_DATABASE_URL). Refuses any URL that does not
    name a PostgreSQL database, and books this engine does not own."""
    url = require_postgresql(url) if url else database_url()
    engine = create_engine(url, future=True)
    event.listen(engine, "begin", _postgres_begin)
    refuse_foreign_books(engine)
    return engine


def holds_tables(engine: Engine) -> bool:
    """Whether the database already holds any table."""
    with read_only(engine) as conn:
        return bool(inspect(conn).get_table_names())


def mark_demo(engine: Engine, mark: str = DEMO_MARK) -> None:
    with engine.begin() as conn:
        if not conn.execute(select(owner.c.mark).where(owner.c.mark == mark)).first():
            conn.execute(owner.insert().values(mark=mark))


def safe_url(url: str) -> str:
    """A URL fit to print: driver, host, port and database only; no user,
    password or query parameters (which can carry credentials)."""
    from sqlalchemy.engine import make_url

    u = make_url(url)
    return f"{u.drivername}://{u.host or ''}{':' + str(u.port) if u.port else ''}/{u.database or ''}"


def create_all(engine: Engine) -> None:
    # import for side effects: every module registers its tables on `metadata`
    from . import models  # noqa: F401
    from .plugins import load_plugins

    from .migrate import upgrade

    load_plugins()
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": SETUP_LOCK_KEY})  # one at a time
        upgrade(conn)  # core's tables, by migration (plan section 10); each plugin's come with its install
        if not conn.execute(select(owner.c.mark)).first():
            conn.execute(owner.insert().values(mark=OWNER_MARK))
        if not conn.execute(select(books_identity.c.books_id)).first():  # written once, at the books' first init
            conn.execute(books_identity.insert().values(id=1, books_id=str(uuid.uuid4())))


def books_id(conn) -> str:
    """The books' identity (a UUID), written once when they were initialised."""
    return conn.execute(select(books_identity.c.books_id).where(books_identity.c.id == 1)).scalar_one()
