"""Old-book refusal preserves the original schema, revision and all data."""
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from erpclaw_core.migrate import upgrade
from erpclaw_core.db import OWNER_MARK
from conftest import drop_everything
from test_revision_0006 import _rows

def schema_objects(conn):
    """Capture non-internal constraints, indexes and triggers alongside column definitions."""
    constraints = conn.execute(text(
        "SELECT c.relname, x.conname, pg_get_constraintdef(x.oid) "
        "FROM pg_constraint x JOIN pg_class c ON c.oid = x.conrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
        "ORDER BY c.relname, x.conname")).all()
    indexes = conn.execute(text(
        "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
        "ORDER BY tablename, indexname")).all()
    triggers = conn.execute(text(
        "SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t "
        "JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND NOT t.tgisinternal ORDER BY c.relname, t.tgname")).all()
    return constraints, indexes, triggers


def columns(conn, tables):
    return {table: [(c["name"], str(c["type"]), c["nullable"], c.get("default"),
                     c.get("identity"), c.get("computed"))
                    for c in inspect(conn).get_columns(table)] for table in tables}


@pytest.mark.parametrize(("marker", "revision"), [
    ("company", "0003"), ("company", "0010"),
    ("mark", "0004"), ("mark", "0010"),
    ("identity", "0006"), ("identity", "0010"),
])
def test_format2_refuses_old_books_without_changing_them(engine, marker, revision):
    drop_everything(engine)
    with engine.begin() as conn:
        upgrade(conn, revision)
        if marker == "company":
            conn.execute(text("INSERT INTO company (id, name, functional_currency, fiscal_year_start_month, created_at) "
                              "VALUES ('old1', 'Old Co', 'USD', 1, CURRENT_TIMESTAMP)"))
        elif marker == "mark":
            conn.execute(text("INSERT INTO erpclaw_books (mark) VALUES (:mark)"), {"mark": OWNER_MARK})
        else:
            conn.execute(text("INSERT INTO books_identity (id, books_id) "
                              "VALUES (1, '11111111-1111-4111-8111-111111111111')"))
        before_rows = _rows(conn)
        tables = inspect(conn).get_table_names()
        before_columns = columns(conn, tables)
        before_objects = schema_objects(conn)
    with pytest.raises(DBAPIError, match="older books are disposable and cannot be upgraded to ledger format 2"):
        with engine.begin() as conn:
            upgrade(conn)
    with engine.connect() as conn:
        assert _rows(conn) == before_rows
        assert inspect(conn).get_table_names() == tables
        assert columns(conn, tables) == before_columns
        assert schema_objects(conn) == before_objects


@pytest.mark.parametrize("isolation", ["READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"])
def test_format2_waits_for_writer_then_refuses_and_preserves_committed_company(engine, isolation):
    """A concurrent pre-format-2 company cannot slip past the emptiness check."""
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    drop_everything(engine)
    with engine.begin() as conn:
        upgrade(conn, "0010")
        before_tables = inspect(conn).get_table_names()
        before_columns = columns(conn, before_tables)
        before_objects = schema_objects(conn)

    started = threading.Event()
    state = {}
    def migrate():
        try:
            with engine.connect().execution_options(isolation_level=isolation) as conn:
                with conn.begin():
                    conn.exec_driver_sql("SET LOCAL lock_timeout = '8s'")
                    state["pid"] = conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                    # Establish any repeatable-read snapshot while the concurrent insert is uncommitted.
                    conn.exec_driver_sql("SELECT count(*) FROM company").scalar_one()
                    started.set()
                    upgrade(conn)
        except (DBAPIError, RuntimeError) as exc:
            return str(exc)
        return "migration unexpectedly succeeded"

    with engine.connect() as writer:
        tx = writer.begin()
        try:
            writer_pid = writer.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            writer.execute(text(
                "INSERT INTO company (id, name, functional_currency, fiscal_year_start_month, created_at) "
                "VALUES ('race1', 'Concurrent Co', 'USD', 1, CURRENT_TIMESTAMP)"))
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(migrate)
                assert started.wait(5)
                if isolation == "READ COMMITTED":
                    deadline = time.monotonic() + 5
                    waiting = False
                    with engine.connect() as observer:
                        while time.monotonic() < deadline and not future.done():
                            observer.exec_driver_sql("SELECT pg_stat_clear_snapshot()")
                            waiting = observer.execute(text(
                                "SELECT wait_event_type = 'Lock' AND :writer_pid = ANY(pg_blocking_pids(pid)) "
                                "FROM pg_stat_activity WHERE pid = :pid"),
                                {"pid": state["pid"], "writer_pid": writer_pid}).scalar_one_or_none() is True
                            if waiting:
                                break
                            time.sleep(.05)
                    assert waiting, "migration did not block on the concurrent writer"
                    tx.commit()
                    outcome = future.result(timeout=10)
                    assert "older books are disposable and cannot be upgraded to ledger format 2" in outcome
                else:
                    # Reject a potentially stale snapshot without waiting for the writer.
                    outcome = future.result(timeout=5)
                    assert "ledger format 2 migration requires READ COMMITTED isolation" in outcome
                    tx.commit()
        finally:
            if tx.is_active:
                tx.rollback()

    with engine.connect() as conn:
        assert conn.execute(text("SELECT name FROM company WHERE id = 'race1'")).scalar_one() == "Concurrent Co"
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0010"
        assert inspect(conn).get_table_names() == before_tables
        assert columns(conn, before_tables) == before_columns
        assert schema_objects(conn) == before_objects


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_caught_isolation_refusal_cannot_commit_earlier_revisions(engine, isolation):
    """Even a caller that catches the refusal cannot commit partial earlier migrations."""
    drop_everything(engine)
    with engine.begin() as conn:
        upgrade(conn, "0003")
        before_rows = _rows(conn)
        before_tables = inspect(conn).get_table_names()
        before_columns = columns(conn, before_tables)
        before_objects = schema_objects(conn)
    with engine.connect().execution_options(isolation_level=isolation) as conn:
        with conn.begin():
            conn.exec_driver_sql("SELECT count(*) FROM company").scalar_one()
            with pytest.raises(DBAPIError, match="ledger format 2 migration requires READ COMMITTED isolation"):
                upgrade(conn)
            # Deliberately swallow the database exception and allow __exit__ to
            # attempt commit: PostgreSQL must roll back the failed transaction.
    with engine.connect() as conn:
        assert _rows(conn) == before_rows
        assert inspect(conn).get_table_names() == before_tables
        assert columns(conn, before_tables) == before_columns
        assert schema_objects(conn) == before_objects
