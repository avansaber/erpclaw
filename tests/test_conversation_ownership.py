"""Store ownership migration and durable recovery across the two databases."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import sqlite3
import subprocess
import sys
import threading
import time

import pytest
from sqlalchemy import event, select

from erpclaw_assistant import store
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import ScriptedModel
from erpclaw_core import models as m, principals as pr
from erpclaw_core.setup import create_company


def sqlite_snapshot():
    with sqlite3.connect(store.path()) as conn:
        return {name: conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
                for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")}


def books_snapshot(engine):
    from sqlalchemy import MetaData

    tables = MetaData()
    tables.reflect(engine)
    with engine.connect() as conn:
        return {t.name: sorted((tuple(row) for row in conn.execute(select(t))), key=repr)
                for t in tables.sorted_tables}


def previous_store():
    """Build the previous schema with gaps and clocks that would reorder a second seq backfill."""
    p = store.path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(p) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE store_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO store_version VALUES (4)")
        for ddl in store._V1 + store._V2 + store._V3:
            conn.execute(ddl)
        conn.execute("INSERT INTO capability_demand VALUES ('date', 'company', 'none', NULL, 'original')")
        for cid in ("old", "empty"):
            conn.execute("INSERT INTO conversation (id) VALUES (?)", (cid,))
        conn.execute("INSERT INTO request (id, conversation_id, token, state, created_at) "
                     "VALUES ('request', 'old', 'token', 'open', 'date')")
        conn.execute("INSERT INTO request_message (conversation_id, request_id, turn_id, text, choice, at) "
                     "VALUES ('old', 'request', 'turn', 'café \\n', 'choice', '2026-01-01')")
        conn.execute("INSERT INTO request_event (request_id, turn_id, event, at) "
                     "VALUES ('request', 'turn', ' {\"type\": \"notice\"} ', '2026-01-02')")
        store._migrate_seq(conn)
        conn.execute("UPDATE conversation SET last_seq = 17 WHERE id = 'old'")
        conn.execute("INSERT INTO request_message (conversation_id, turn_id, text, at, seq) "
                     "VALUES ('old', 'later', 'unassigned', '1900-01-01', 17)")
        conn.execute("INSERT INTO message_resolution (message_id, completed, field) VALUES (1, 1, 'customer')")
    store._ready.discard(str(p))
    return sqlite_snapshot()


def owner_id(engine):
    with engine.connect() as conn:
        return pr.owner(conn)["id"]


def audits(engine):
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(m.audit_log).where(
            m.audit_log.c.action == "assistant.legacy_owner_assigned")).mappings()]


def receipt():
    with sqlite3.connect(store.path()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM store_receipt WHERE step = 'legacy_owner'").fetchone()
        return dict(row) if row else None


def test_start_without_store_keeps_creation_lazy_and_writes_no_audit(engine):
    p = store.path()
    assert not p.parent.exists()
    before_books = books_snapshot(engine)
    Assistant(engine, ScriptedModel([]))
    assert not p.parent.exists()
    assert audits(engine) == []
    assert books_snapshot(engine) == before_books
    Assistant(engine, ScriptedModel([]))
    assert not p.parent.exists()
    assert books_snapshot(engine) == before_books

    store.create_conversation("new", owner_id(engine))
    Assistant(engine, ScriptedModel([]))
    assert receipt()["assigned_count"] == 0 and receipt()["audited_at"]
    assert books_snapshot(engine) == before_books
    before_store = sqlite_snapshot()
    Assistant(engine, ScriptedModel([]))
    assert sqlite_snapshot() == before_store
    assert books_snapshot(engine) == before_books


def test_previous_version_migration_preserves_every_row_and_sequence():
    """Catches skipped migration, seq reallocation, and non-guarded column additions."""
    before = previous_store()
    assert store.version() == store.VERSION == 5
    with sqlite3.connect(store.path()) as conn:
        for table, rows in before.items():
            if table == "store_version":
                continue
            # New columns are appended, so every old value must survive in place.
            got = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            if rows:
                assert [r[:len(rows[0])] for r in got] == rows
            else:
                assert got == []
        assert [r[0] for r in conn.execute("SELECT owner_principal_id FROM conversation")] == [None, None]
        assert conn.execute("SELECT author_principal_id, author_name FROM request_message").fetchall() == [(None, None)] * 2
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(conversation)")}
        assert "ix_conversation_owner" in indexes
        assert [r[2] for r in conn.execute("PRAGMA index_info(ix_conversation_owner)")] == ["owner_principal_id"]
        assert [r[2] for r in conn.execute("PRAGMA index_info(ix_request_message_conversation)")] == ["conversation_id", "id"]
        assert [r[1] for r in conn.execute("PRAGMA table_info(conversation_participant)")] == [
            "conversation_id", "principal_id", "added_at"]
        assert conn.execute("SELECT * FROM conversation_participant").fetchall() == []
    once = sqlite_snapshot()
    store._ready.discard(str(store.path()))
    assert store.version() == 5
    assert sqlite_snapshot() == once


def test_two_processes_migrate_previous_version_without_changing_rows():
    """Catches unguarded DDL and migration transactions that interleave."""
    previous_store()
    script = "from erpclaw_assistant import store; assert store.version() == 5"
    procs = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for _ in range(2)]
    for proc in procs:
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, (out, err)
    once = sqlite_snapshot()
    assert store.version() == 5
    assert sqlite_snapshot() == once


def test_legacy_assignment_receipt_and_audit_are_durable_and_second_start_is_noop(engine):
    """Catches omitted receipts, repeated assignment, and repeated audits."""
    previous_store()
    pid = owner_id(engine)
    store.assign_legacy_owner(engine, pid)
    saved = receipt()
    assert saved["owner_principal_id"] == pid and saved["assigned_count"] == 2
    assert saved["assigned_at"] and saved["audited_at"]
    with sqlite3.connect(store.path()) as conn:
        assert conn.execute("SELECT id, owner_principal_id FROM conversation ORDER BY id").fetchall() == [
            ("empty", pid), ("old", pid)]
        assert conn.execute("SELECT author_principal_id, author_name FROM request_message").fetchall() == [(None, None)] * 2
    [row] = audits(engine)
    assert row["actor"] == "engine"
    assert json.loads(row["detail"]) == {"step": "legacy_owner", "owner_principal_id": pid, "assigned_count": 2}
    store.create_conversation("second", "second-principal")
    before_store, before_books = sqlite_snapshot(), books_snapshot(engine)
    store.assign_legacy_owner(engine, pid)
    assert sqlite_snapshot() == before_store
    assert books_snapshot(engine) == before_books
    store.release(store.claim("old", "continue", principal_id=pid, principal_name="owner", create=False))


@pytest.mark.parametrize("same_creation_time", [False, True])
def test_start_with_two_companies_audits_once_and_second_start_writes_nothing(engine, same_creation_time):
    previous_store()
    with engine.connect() as conn:
        first = conn.execute(select(m.company)).mappings().one()
    second = create_company(engine, "Second Company")
    if same_creation_time:
        with engine.begin() as conn:
            conn.execute(m.company.update().where(m.company.c.id == second).values(created_at=first["created_at"]))
    expected_company = min(first["id"], second) if same_creation_time else first["id"]
    before_books = books_snapshot(engine)
    Assistant(engine, ScriptedModel([]))
    [row] = audits(engine)
    assert row["company_id"] == expected_company and row["actor"] == "engine"
    assert json.loads(row["detail"]) == {
        "step": "legacy_owner", "owner_principal_id": owner_id(engine), "assigned_count": 2}
    after_books = books_snapshot(engine)
    assert {name: rows for name, rows in after_books.items() if name != "audit_log"} == {
        name: rows for name, rows in before_books.items() if name != "audit_log"}
    assert after_books["audit_log"] == sorted(
        before_books["audit_log"] + [tuple(row[c.name] for c in m.audit_log.columns)], key=repr)
    before_store = sqlite_snapshot()
    Assistant(engine, ScriptedModel([]))
    assert sqlite_snapshot() == before_store
    assert books_snapshot(engine) == after_books


class Interrupted(BaseException):
    pass


@pytest.mark.parametrize("step", [1, 2, 3])
@pytest.mark.parametrize("assigned_count", [0, 2])
def test_restart_after_each_committed_step_finishes_exactly_once(engine, monkeypatch, step, assigned_count):
    """Catches non-durable receipts, missing audit lookup, and skipped recovery marking."""
    pid = owner_id(engine)
    if assigned_count:
        previous_store()
    else:
        store.create_conversation("owned", pid)
    before_books = books_snapshot(engine)
    real_tx, real_begin = store._tx, engine.begin
    count = 0

    @contextmanager
    def interrupted_tx():
        nonlocal count
        with real_tx() as conn:
            yield conn
        count += 1
        if (step == 1 and count == 1) or (step == 3 and count == 2):
            raise Interrupted()

    @contextmanager
    def interrupted_begin():
        with real_begin() as conn:
            yield conn
        if step == 2:
            raise Interrupted()

    with monkeypatch.context() as patch:
        patch.setattr(store, "_tx", interrupted_tx)
        patch.setattr(engine, "begin", interrupted_begin)
        with pytest.raises(Interrupted):
            store.assign_legacy_owner(engine, pid)
    assert receipt()["assigned_count"] == assigned_count
    assert len(audits(engine)) == (0 if step == 1 or assigned_count == 0 else 1)
    assert bool(receipt()["audited_at"]) == (step == 3)
    # Construction, rather than a turn or an explicit recovery call, must finish recovery.
    Assistant(engine, ScriptedModel([]))
    assert receipt()["audited_at"]
    assert len(audits(engine)) == (1 if assigned_count else 0)
    if assigned_count == 0:
        assert books_snapshot(engine) == before_books
    before_store, before_books = sqlite_snapshot(), books_snapshot(engine)
    Assistant(engine, ScriptedModel([]))
    assert sqlite_snapshot() == before_store
    assert books_snapshot(engine) == before_books


def test_concurrent_start_recovery_has_one_audit_under_postgresql_lock(engine, monkeypatch):
    """Catches removal of the advisory lock: both unprotected lookups then insert an audit."""
    create_company(engine, "Second Company")
    previous_store()
    pid = owner_id(engine)
    real_tx = store._tx

    @contextmanager
    def stop_after_assignment():
        with real_tx() as conn:
            yield conn
        raise Interrupted()

    with monkeypatch.context() as patch:
        patch.setattr(store, "_tx", stop_after_assignment)
        with pytest.raises(Interrupted):
            store.assign_legacy_owner(engine, pid)
    assert not audits(engine) and receipt()["audited_at"] is None
    barrier = threading.Barrier(2)
    real_begin = engine.begin

    @contextmanager
    def simultaneous_begin():
        with real_begin() as conn:
            # Both starters arrive BEFORE the lock, never after its protected lookup.
            barrier.wait(timeout=10)
            yield conn

    def delay_lookup(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "audit_log" in statement:
            # Widen the unlocked lookup/insert race without putting a barrier behind the lock.
            time.sleep(0.2)

    with monkeypatch.context() as patch:
        patch.setattr(engine, "begin", simultaneous_begin)
        event.listen(engine, "after_cursor_execute", delay_lookup)
        try:
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures = [workers.submit(Assistant, engine, ScriptedModel([])) for _ in range(2)]
                for future in futures:
                    future.result(timeout=30)
        finally:
            event.remove(engine, "after_cursor_execute", delay_lookup)
    assert len(audits(engine)) == 1
    assert receipt()["audited_at"]
