"""Trial resources close even while a failed turn's traceback is retained."""
import gc
import logging
import secrets
import threading
import time
from contextlib import contextmanager

import psycopg
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from conftest import PG_URL
from erpclaw_assistant.model import Reply, ScriptedModel
from erpclaw_cli.evals import run as ev


class ModelTimedOut(BaseException):
    pass


class TimingOut:
    name = "timing-out"

    def __init__(self, books, retain_connection, before_timeout=None):
        self.books = books
        self.retain_connection = retain_connection
        self.connection = None
        self.traceback = None
        self.error = ModelTimedOut("the model did not answer in time")
        self.before_timeout = before_timeout

    def complete(self, messages, tools):
        # Keep a checked-out runtime connection in the timeout's frame, independently of
        # which request internals this Python version happens to retain in its traceback.
        if self.retain_connection:
            connection = self.books.engines[-1].connect()
            connection.exec_driver_sql("SELECT 1")
            self.connection = connection
        if self.before_timeout is not None:
            self.before_timeout()
        try:
            raise self.error
        except ModelTimedOut as exc:
            self.traceback = exc.__traceback__
            raise


@pytest.fixture
def disposable_books(monkeypatch):
    """Use the documented eval privileges, with a superuser only for test teardown."""
    monkeypatch.delenv("ERPCLAW_ALLOW_OWNER_RUNTIME")
    login, password = f"erpclaw_evaladm_{secrets.token_hex(3)}", secrets.token_hex(12)
    root = create_engine(PG_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with root.connect() as conn:
        conn.exec_driver_sql(f"CREATE ROLE \"{login}\" LOGIN CREATEDB CREATEROLE PASSWORD '{password}'")
    url = make_url(PG_URL).set(username=login, password=password).render_as_string(hide_password=False)
    opened = []

    def new_books():
        books = ev.EvalBooks(url)
        opened.append(books)
        books.seed()
        return books

    try:
        yield new_books, root
    finally:
        for books in opened:
            for engine in books.engines:
                engine.dispose()
            books.admin.dispose()
            with root.connect() as conn:
                names = conn.exec_driver_sql("SELECT datname FROM pg_database WHERE datname LIKE %(p)s",
                                             {"p": books.prefix + "%"}).scalars().all()
                for name in names:
                    conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
                conn.exec_driver_sql(f'DROP ROLE IF EXISTS "{books.role}"')
        with root.connect() as conn:
            conn.exec_driver_sql(f'DROP ROLE IF EXISTS "{login}"')
        root.dispose()


def assert_dropped(books, root):
    with root.connect() as conn:
        databases = conn.exec_driver_sql("SELECT datname FROM pg_database WHERE datname LIKE %(p)s",
                                         {"p": books.prefix + "%"}).scalars().all()
        role = conn.exec_driver_sql("SELECT 1 FROM pg_roles WHERE rolname = %(r)s", {"r": books.role}).first()
    assert databases == []
    assert role is None


def failed_trial(books, retain_connection=True):
    model = TimingOut(books, retain_connection)
    with pytest.raises(ModelTimedOut, match="the model did not answer in time") as raised:
        ev.run_trial(ev.CASES[0], books, model)
    assert raised.value is model.error
    if retain_connection:
        assert model.connection.closed
    return model.traceback  # retain the original frames through books.close(), even with GC enabled


@pytest.mark.parametrize("retain_connection", [False, True])
def test_failed_trial_cleans_up_with_cycle_collection_disabled(disposable_books, retain_connection):
    new_books, root = disposable_books
    enabled = gc.isenabled()
    gc.disable()
    try:
        books = new_books()
        traceback = failed_trial(books, retain_connection)
        books.close()
        assert_dropped(books, root)
        assert traceback is not None
    finally:
        if enabled:
            gc.enable()


def test_failed_trial_cleans_up_twenty_times(disposable_books):
    new_books, root = disposable_books
    assert gc.isenabled()
    tracebacks = []
    for _ in range(20):
        books = new_books()
        tracebacks.append(failed_trial(books))
        books.close()
        assert_dropped(books, root)


def test_passing_trial_still_cleans_up(disposable_books):
    new_books, root = disposable_books
    books = new_books()
    result = ev.run_trial(ev.Case("reply", "hello", "read", expect=["Hello"]), books,
                          ScriptedModel([Reply("Hello.")]))
    assert result["ok"], result
    assert books.engines == []
    assert books.made == [books.pristine]
    books.close()
    assert_dropped(books, root)


@contextmanager
def foreign_session(books, root):
    role, password = f"erpclaw_foreign_{secrets.token_hex(4)}", secrets.token_hex(12)
    with root.connect() as conn:
        conn.exec_driver_sql(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}'")
    connection = None
    try:
        url = make_url(PG_URL).set(drivername="postgresql", username=role, password=password, database=books.pristine)
        connection = psycopg.connect(url.render_as_string(hide_password=False), autocommit=True,
                                     application_name="eval-cleanup-foreign")
        connection.execute("SELECT 1").fetchone()
        yield role, connection
    finally:
        with root.connect() as conn:
            conn.exec_driver_sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %(role)s",
                                 {"role": role})
        if connection is not None:
            connection.close()
        with root.connect() as conn:
            conn.exec_driver_sql(f'DROP ROLE "{role}"')


def test_close_waits_for_foreign_session_to_end(disposable_books):
    new_books, root = disposable_books
    books = new_books()
    with foreign_session(books, root) as (_, connection):
        closer = threading.Timer(1.0, connection.close)
        closer.start()
        started = time.monotonic()
        try:
            books.close()
            elapsed = time.monotonic() - started
            assert 0.75 <= elapsed < 15
            assert_dropped(books, root)
        finally:
            closer.join()


@pytest.mark.parametrize("read_all_stats", [False, True])
def test_close_reports_persistent_foreign_session(disposable_books, monkeypatch, caplog, read_all_stats):
    new_books, root = disposable_books
    books = new_books()
    monkeypatch.setattr(ev, "DROP_DATABASE_RETRY_WINDOW", 2.0, raising=False)
    if read_all_stats:
        with root.connect() as conn:
            conn.exec_driver_sql(f'GRANT pg_read_all_stats TO "{books.url.username}"')
    drop_errors = []
    attempted = []

    def record_error(context):
        if context.statement.startswith("DROP DATABASE"):
            drop_errors.append(context.sqlalchemy_exception)

    def record_statement(conn, cursor, statement, parameters, context, executemany):
        attempted.append(statement)

    event.listen(books.admin, "handle_error", record_error)
    event.listen(books.admin, "before_cursor_execute", record_statement)
    try:
        with foreign_session(books, root) as (role, _):
            with caplog.at_level(logging.WARNING, logger=ev.__name__):
                started = time.monotonic()
                with pytest.raises(DBAPIError) as raised:
                    books.close()
                elapsed = time.monotonic() - started
            assert isinstance(raised.value.orig, psycopg.errors.InsufficientPrivilege)
            assert raised.value is drop_errors[0]
            assert len(drop_errors) > 1
            assert 2 <= elapsed < 7
            assert attempted[-1] == f'DROP ROLE IF EXISTS "{books.role}"'
            diagnostics = [record for record in caplog.records
                           if record.name == ev.__name__ and role in record.getMessage()]
            assert len(diagnostics) == 1
            message = diagnostics[0].getMessage()
            assert diagnostics[0].levelno == logging.WARNING
            assert "\n" not in message
            assert books.pristine in message
            assert all(field in message for field in
                       ("backend_type", "usename", "state", "application_name", "state_age_seconds", "query"))
            if read_all_stats:
                assert "idle" in message
                assert "client backend" in message
                assert "eval-cleanup-foreign" in message
                assert "SELECT 1" in message
            else:
                assert "state=None" in message
    finally:
        event.remove(books.admin, "handle_error", record_error)
        event.remove(books.admin, "before_cursor_execute", record_statement)
        books.close()
    assert_dropped(books, root)


def test_database_drop_does_not_retry_other_database_errors(disposable_books, monkeypatch):
    new_books, root = disposable_books
    books = new_books()
    attempts = []

    def record_drop(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("DROP DATABASE"):
            attempts.append(statement)

    admin = create_engine(books.url.set(database=books.pristine), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    event.listen(admin, "before_cursor_execute", record_drop)
    try:
        # PostgreSQL always refuses to drop the database of this very connection.
        with monkeypatch.context() as patch:
            patch.setattr(books, "admin", admin)
            with pytest.raises(DBAPIError) as raised:
                books._drop_database(books.pristine)
        assert isinstance(raised.value.orig, psycopg.errors.ObjectInUse)
        assert len(attempts) == 1
    finally:
        admin.dispose()
        books.close()
    assert_dropped(books, root)


def test_database_drop_diagnostic_failure_preserves_original_error(disposable_books, monkeypatch):
    new_books, root = disposable_books
    books = new_books()
    monkeypatch.setattr(ev, "DROP_DATABASE_RETRY_WINDOW", 0.0, raising=False)
    drop_errors = []
    diagnostics = []

    def record_error(context):
        if context.statement.startswith("DROP DATABASE"):
            drop_errors.append(context.sqlalchemy_exception)

    def fail_diagnostic(conn, cursor, statement, parameters, context, executemany):
        if "pg_stat_activity" in statement:
            diagnostics.append(statement)
            raise RuntimeError("cannot read session activity")

    event.listen(books.admin, "handle_error", record_error)
    event.listen(books.admin, "before_cursor_execute", fail_diagnostic)
    try:
        with foreign_session(books, root):
            with pytest.raises(DBAPIError) as raised:
                books.close()
            assert isinstance(raised.value.orig, psycopg.errors.InsufficientPrivilege)
            assert raised.value is drop_errors[0]
            assert len(diagnostics) == 1
    finally:
        event.remove(books.admin, "handle_error", record_error)
        event.remove(books.admin, "before_cursor_execute", fail_diagnostic)
        books.close()
    assert_dropped(books, root)


def terminate_connection(connection, admin):
    pid = connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
    with admin.connect() as conn:
        assert conn.exec_driver_sql("SELECT pg_terminate_backend(%(pid)s)", {"pid": pid}).scalar_one()


def test_terminated_runtime_session_preserves_model_timeout(disposable_books, caplog):
    new_books, root = disposable_books
    books = new_books()
    model = TimingOut(books, True)
    model.before_timeout = lambda: terminate_connection(model.connection, root)
    try:
        with pytest.raises(ModelTimedOut) as raised:
            ev.run_trial(ev.CASES[0], books, model)
        assert raised.value is model.error
        assert model.connection.closed
        assert books.made == [books.pristine]
    finally:
        books.close()
    assert_dropped(books, root)
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize("original_failure", [False, True])
def test_dispose_failure_still_drops_trial_database(disposable_books, monkeypatch, original_failure):
    new_books, root = disposable_books
    books = new_books()
    real_make_engine = ev.make_engine
    disposal_error = RuntimeError("engine disposal failed")
    disposed = []

    def make_engine(url):
        engine = real_make_engine(url)

        def dispose():
            disposed.append(engine)
            raise disposal_error

        patch.setattr(engine, "dispose", dispose)
        return engine

    try:
        with monkeypatch.context() as patch:
            patch.setattr(ev, "make_engine", make_engine)
            model = TimingOut(books, True) if original_failure else ScriptedModel([Reply("Hello.")])
            expected = model.error if original_failure else disposal_error
            with pytest.raises(type(expected)) as raised:
                ev.run_trial(ev.Case("reply", "hello", "read", expect=["Hello"]), books, model)
            assert raised.value is expected
            assert len(disposed) == 1
            if original_failure:
                assert model.connection.closed
            with root.connect() as conn:
                assert conn.exec_driver_sql("SELECT 1 FROM pg_database WHERE datname = %(name)s",
                                            {"name": disposed[0].url.database}).first() is None
    finally:
        books.close()
    assert_dropped(books, root)


def test_terminated_runtime_session_without_trial_failure_is_ignored(disposable_books, caplog):
    new_books, root = disposable_books
    books = new_books()
    engine = books.trial()
    connection = engine.connect()
    terminate_connection(connection, root)
    books.finish(engine)
    books.close()
    assert connection.closed
    assert_dropped(books, root)
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize("original_failure", [False, True])
def test_client_close_failure_still_cleans_up(disposable_books, monkeypatch, original_failure):
    from fastapi.testclient import TestClient

    new_books, root = disposable_books
    books = new_books()
    close_error = RuntimeError("client close failed")
    real_close = TestClient.close

    def close(client):
        real_close(client)
        raise close_error

    monkeypatch.setattr(TestClient, "close", close)
    model = TimingOut(books, True) if original_failure else ScriptedModel([Reply("Hello.")])
    expected = model.error if original_failure else close_error
    try:
        with pytest.raises(type(expected)) as raised:
            ev.run_trial(ev.Case("reply", "hello", "read", expect=["Hello"]), books, model)
        assert raised.value is expected
        assert books.engines == []
        assert books.made == [books.pristine]
    finally:
        books.close()
    assert_dropped(books, root)


def test_close_attempts_all_engines_and_drops_after_connection_error(disposable_books, monkeypatch):
    new_books, root = disposable_books
    books = new_books()
    engines = [books.trial(), books.trial()]
    connections = [engine.connect() for engine in engines]
    for connection in connections:
        connection.exec_driver_sql("SELECT 1")
    close_error = RuntimeError("connection close failed")
    # Register the failing close before the engine records the connection's callback.
    extra = None
    with monkeypatch.context() as patch:
        from sqlalchemy.engine import Connection

        original_close = Connection.close

        def failing_close(connection):
            original_close(connection)
            if connection is extra:
                raise close_error

        patch.setattr(Connection, "close", failing_close)
        extra = engines[0].connect()
        extra.exec_driver_sql("SELECT 1")
        with pytest.raises(RuntimeError) as raised:
            books.close()
        assert raised.value is close_error
    assert all(connection.closed for connection in [*connections, extra])
    assert_dropped(books, root)


def test_close_attempts_remaining_drops_after_database_drop_error(disposable_books):
    new_books, root = disposable_books
    books = new_books()
    engine = books.trial()
    drop_error = RuntimeError("database drop failed")
    attempted = []

    def fail_first_drop(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("DROP "):
            attempted.append(statement)
            if len(attempted) == 1:
                raise drop_error

    event.listen(books.admin, "before_cursor_execute", fail_first_drop)
    try:
        with pytest.raises(RuntimeError) as raised:
            books.close()
        assert raised.value is drop_error
        assert attempted == [f'DROP DATABASE IF EXISTS "{engine.url.database}" WITH (FORCE)',
                             f'DROP DATABASE IF EXISTS "{books.pristine}" WITH (FORCE)',
                             f'DROP ROLE IF EXISTS "{books.role}"']
    finally:
        event.remove(books.admin, "before_cursor_execute", fail_first_drop)
        books.close()
    assert_dropped(books, root)
