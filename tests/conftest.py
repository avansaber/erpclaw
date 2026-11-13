import atexit
import os
import re
import secrets
import sys

import pytest

PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")
if not PG_URL:
    raise RuntimeError("The suite runs on PostgreSQL: set ERPCLAW_PG_TEST_URL to a *_test database's URL")


def _check_pg_test_url(raw: str) -> None:
    """The fixture drops every table, so only a database named *_test may be
    targeted, and nothing in the URL may redirect the connection elsewhere."""
    from sqlalchemy.engine import make_url

    url = make_url(raw)
    overrides = {k.lower() for k in url.query} & {"dbname", "database", "service", "options", "host", "hostaddr", "port"}
    if not (url.database or "").endswith("_test") or overrides:
        raise RuntimeError("ERPCLAW_PG_TEST_URL must name a *_test database and carry no connection overrides")


_check_pg_test_url(PG_URL)


def _per_run_database_name(base: str) -> str:
    """An ASCII identifier with the entire unique suffix inside PostgreSQL's byte limit."""
    suffix = f"_r{os.getpid()}_{secrets.token_hex(4)}_test"
    prefix = re.sub(r"[^a-zA-Z0-9_]", "_", base.removesuffix("_test"))
    if prefix[:1].isdigit():
        prefix = "_" + prefix
    return prefix[:63 - len(suffix)] + suffix


_PER_RUN_BASE_URL = PG_URL
_PER_RUN_DATABASE = None


def _cleanup_per_run_database() -> None:
    """Drop only the database this process successfully created; retry at exit on failure."""
    global _PER_RUN_DATABASE
    if _PER_RUN_DATABASE is None:
        return
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.pool import NullPool

        admin = create_engine(_PER_RUN_BASE_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
        try:
            with admin.connect() as conn:
                name = conn.dialect.identifier_preparer.quote_identifier(_PER_RUN_DATABASE)
                conn.exec_driver_sql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        finally:
            admin.dispose()
    except Exception as exc:  # cleanup must not change the test outcome
        print(f"Warning: could not drop per-run test database {_PER_RUN_DATABASE!r} "
              f"({type(exc).__name__})", file=sys.stderr)
    else:
        _PER_RUN_DATABASE = None


if os.environ.get("ERPCLAW_TEST_DB_PER_RUN") == "1":
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    base_url = make_url(PG_URL)
    database = _per_run_database_name(base_url.database)
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with admin.connect() as conn:
            name = conn.dialect.identifier_preparer.quote_identifier(database)
            conn.exec_driver_sql(f"CREATE DATABASE {name}")
            _PER_RUN_DATABASE = database
            atexit.register(_cleanup_per_run_database)
    finally:
        admin.dispose()
    PG_URL = base_url.set(database=database).render_as_string(hide_password=False)
    os.environ["ERPCLAW_PG_TEST_URL"] = PG_URL


def pytest_sessionfinish(session, exitstatus):
    _cleanup_per_run_database()


def _shared_database_allowed() -> bool:
    """The test database is shared by every lane of the workspace, and a run drops its tables: the suite opens it only
    while the workspace's suite lock is held (ERPCLAW_LOCKS_HELD names its .lock-suite file), in CI, or when the run
    says its database is its own (ERPCLAW_TEST_DB_PRIVATE=1, a review clone's container database), or creates one
    for this run (ERPCLAW_TEST_DB_PER_RUN=1)."""
    held = [p for p in os.environ.get("ERPCLAW_LOCKS_HELD", "").split(os.pathsep) if p]
    return (any(os.path.basename(p) == ".lock-suite" for p in held) or os.environ.get("GITHUB_ACTIONS") == "true"
            or os.environ.get("ERPCLAW_TEST_DB_PRIVATE") == "1" or _PER_RUN_DATABASE is not None)


if not _shared_database_allowed():
    raise RuntimeError("The test database is shared: run the suite while holding the workspace's suite lock "
                       "(ERPCLAW_LOCKS_HELD names its .lock-suite file), in CI, or with ERPCLAW_TEST_DB_PRIVATE=1 "
                       "for a database of the run's own.")


from erpclaw_core.db import make_engine, metadata
from erpclaw_core.plugins import load_plugins


def drop_everything(eng) -> None:
    """Every table of the books, the migration version table included, and the
    engine's trigger functions."""
    from sqlalchemy import MetaData

    present = MetaData()  # what the database holds, also tables the models no longer have (a test stopped at
    present.reflect(eng)  # an earlier revision), dropped in their own dependency order
    present.drop_all(eng)
    with eng.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
        for name in conn.exec_driver_sql(
                "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = current_schema() AND p.proname LIKE 'erpclaw%%'").scalars().all():
            conn.exec_driver_sql(f'DROP FUNCTION IF EXISTS "{name}" CASCADE')


def _planned_state():
    """What this release switches off (Contract.planned, Metric.planned, the sales plugin's email): read once."""
    import erpclaw_sales
    from erpclaw_core import outbox

    load_plugins()
    from erpclaw_core.plugins import REGISTRY

    return ({n: c.planned for n, c in REGISTRY.contracts.items()}, {n: m.planned for n, m in REGISTRY.metrics.items()},
            (erpclaw_sales.EMAIL_PLANNED, outbox.PLANNED))


RELEASED = _planned_state()


def _set_planned(monkeypatch, state) -> None:
    import erpclaw_sales
    from erpclaw_core import outbox
    from erpclaw_core.plugins import REGISTRY

    contracts, metrics, (email, delivery) = state
    for n, c in REGISTRY.contracts.items():
        monkeypatch.setattr(c, "planned", contracts[n])
    for n, mt in REGISTRY.metrics.items():
        monkeypatch.setattr(mt, "planned", metrics[n])
    monkeypatch.setattr(erpclaw_sales, "EMAIL_PLANNED", email)
    monkeypatch.setattr(outbox, "PLANNED", delivery)


@pytest.fixture(autouse=True)
def every_feature(monkeypatch):
    """The suite tests every operation's code, those this release switches off included (they come back on in a
    later release); tests of the release as shipped use `as_released`."""
    contracts, metrics, _ = RELEASED
    _set_planned(monkeypatch, ({n: None for n in contracts}, {n: None for n in metrics}, (None, None)))


@pytest.fixture
def as_released(monkeypatch):
    """The engine as this release ships it: the planned operations off."""
    _set_planned(monkeypatch, RELEASED)


@pytest.fixture(autouse=True)
def pinned(monkeypatch, tmp_path):
    monkeypatch.setenv("ERPCLAW_TODAY", "2026-10-26")
    monkeypatch.setenv("ERPCLAW_MAIL_DIR", str(tmp_path / "mail"))
    monkeypatch.delenv("ERPCLAW_DATABASE_URL", raising=False)
    monkeypatch.delenv("ERPCLAW_MIGRATION_URL", raising=False)
    # The suite connects as the test database's owner; the runtime role tests
    # unset this and connect as a genuine runtime login (runtime role spec).
    monkeypatch.setenv("ERPCLAW_ALLOW_OWNER_RUNTIME", "1")
    # Any default path a test reaches lands in its own temp dir, never in real books.
    monkeypatch.setenv("ERPCLAW_HOME", str(tmp_path / "home"))


def empty_books():
    """The test database, emptied: its name checked on the server before anything is dropped."""
    eng = make_engine(PG_URL)
    with eng.connect() as conn:
        actual = conn.exec_driver_sql("SELECT current_database()").scalar()
    if not actual.endswith("_test"):
        raise RuntimeError(f"connected to {actual!r}, not a *_test database")
    drop_everything(eng)
    return eng


@pytest.fixture
def engine():
    from erpclaw_sales.demo import seed_demo

    load_plugins()
    eng = empty_books()
    seed_demo(eng)
    yield eng
    drop_everything(eng)
    eng.dispose()


@pytest.fixture
def runtime_login(engine, monkeypatch):
    """A genuine runtime login role, created by the test database's owner, with
    the books' grants applied; yields its URL, and the owner checks are on
    (runtime role spec)."""
    from sqlalchemy.engine import make_url

    from erpclaw_core.grants import apply_grants

    role, password = f"erpclaw_rt_{secrets.token_hex(4)}", secrets.token_hex(16)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
        apply_grants(conn, role)
    monkeypatch.delenv("ERPCLAW_ALLOW_OWNER_RUNTIME")
    yield make_url(PG_URL).set(username=role, password=password).render_as_string(hide_password=False)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{role}'")
        conn.exec_driver_sql(f"DROP OWNED BY {role}")
        conn.exec_driver_sql(f"DROP ROLE {role}")
        conn.exec_driver_sql("GRANT USAGE ON SCHEMA public TO PUBLIC")  # the default apply_grants revoked


@pytest.fixture
def other_database():
    """A second, new, empty test database on the same server, removed afterwards; yields its URL."""
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    name = f"erpclaw_other_{secrets.token_hex(4)}_test"
    admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    try:
        yield make_url(PG_URL).set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.dispose()


def cli_urls(monkeypatch, engine, runtime_login) -> None:
    """The CLI's environment: the runtime login for the runtime and the test
    database's owner for migration."""
    monkeypatch.setenv("ERPCLAW_DATABASE_URL", runtime_login)
    monkeypatch.setenv("ERPCLAW_MIGRATION_URL", engine.url.render_as_string(hide_password=False))
