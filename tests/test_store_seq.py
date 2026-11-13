"""One durable cursor across the assistant store's messages and engine events."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import sqlite3
import subprocess
import sys
import threading

import pytest

from erpclaw_assistant import store
from erpclaw_core import principals as pr


def rows(sql, args=()):
    with sqlite3.connect(store.path()) as conn:
        return conn.execute(sql, args).fetchall()


def last_seq(cid="c"):
    return rows("SELECT last_seq FROM conversation WHERE id = ?", (cid,))[0][0]


def start(engine, cid="c"):
    with engine.connect() as conn:
        claimant = pr.owner(conn)
    c = store.claim(cid, "owner", principal_id=claimant["id"], principal_name=claimant["name"])
    assert c is not None
    req = store.new_request(c)
    assert store.assign(c, req["id"])
    return c, req["id"]


def finish(c, rid, events):
    return store.finalise(c, rid, events, pending_question=None, pending_card=None, state="open", why=None,
                          serial_now=0, bindings_now={}, intent_state="unused")


def test_allocation_interleaves_messages_and_events_across_requests(engine):
    c, rid = start(engine)
    store.add_events(rid, [{"type": "notice", "text": "first"}])
    assert finish(c, rid, [{"type": "answer", "text": "second"}])
    c2, rid2 = start(engine)
    assert rid != rid2
    store.set_pending(rid2, events=[{"type": "ask", "text": "third"}])
    assert rows("SELECT seq FROM request_message ORDER BY id") == [(1,), (4,)]
    assert rows("SELECT seq FROM request_event ORDER BY id") == [(2,), (3,), (5,)]
    assert last_seq() == 5
    assert rows("SELECT conversation_id FROM request_event") == [("c",)] * 3
    assert store.version() == store.VERSION == 5


@pytest.mark.parametrize("writer", ["finalise", "reconcile_outcome", "end_request", "set_pending", "add_events"])
def test_every_event_writer_assigns_seq_and_ignores_transients(engine, writer):
    c, rid = start(engine)
    transient = [{"type": kind, "text": "live"} for kind in ("working", "decision", "delivery")]
    durable = {"type": "notice", "text": writer}
    batch = transient + [durable] + transient
    if writer == "finalise":
        assert finish(c, rid, batch)
    elif writer == "reconcile_outcome":
        store.set_pending(rid, card="card")
        assert store.reconcile_outcome(c, "card", "committed", rid, "ended", events=batch) == "applied"
    elif writer == "end_request":
        assert store.end_request(rid, "done", batch)
    elif writer == "set_pending":
        store.set_pending(rid, events=batch)
    else:
        store.add_events(rid, batch)
    assert store.events(rid) == [durable]
    assert last_seq() == 2
    assert [x["seq"] for x in store.history_page("c")["items"]] == [1, 2]


@pytest.mark.parametrize("writer", ["finalise", "reconcile_outcome", "end_request", "set_pending", "add_events"])
def test_only_transients_never_advance_the_counter(engine, writer):
    c, rid = start(engine)
    batch = [{"type": kind} for kind in ("working", "decision", "delivery")]
    if writer == "finalise":
        assert finish(c, rid, batch)
    elif writer == "reconcile_outcome":
        store.set_pending(rid, card="card")
        assert store.reconcile_outcome(c, "card", "committed", rid, "ended", events=batch) == "applied"
    elif writer == "end_request":
        assert store.end_request(rid, "done", batch)
    elif writer == "set_pending":
        store.set_pending(rid, events=batch)
    else:
        store.add_events(rid, batch)
    assert last_seq() == 1
    assert store.events(rid) == []
    assert store.history_page("c", after=1) == {"items": [], "next": 1}


def test_no_write_retries_and_generation_changes_allocate_nothing(engine, monkeypatch):
    # This head has no client_message_id replay API. Exercise its actual no-write
    # retries and reuse of the stored message through catch_up instead.
    with engine.connect() as conn:
        claimant = pr.owner(conn)
    c, rid = start(engine)
    assert store.claim("c", "busy retry", principal_id=claimant["id"], principal_name=claimant["name"]) is None
    c = store.catch_up(c, c.generation + 3)
    assert c.message_id == rows("SELECT id FROM request_message")[0][0]
    assert last_seq() == 1
    store.set_pending(rid, card="card")
    assert store.reconcile_outcome(c, "card", "done", rid, "ended", events=[{"type": "notice"}]) == "applied"
    store.set_pending(rid, card="card")
    assert store.reconcile_outcome(c, "card", "done", rid, "ended", events=[{"type": "notice"}]) == "incomplete"
    assert last_seq() == 2
    assert finish(c, rid, [{"type": "answer"}])
    assert not finish(c, rid, [{"type": "answer"}])
    assert last_seq() == 3
    assert rows("SELECT count(*) FROM request_message") == [(1,)]
    monkeypatch.setattr(store, "LEASE", timedelta(0))
    stalled = store.claim("c", "stalled", principal_id=claimant["id"], principal_name=claimant["name"])
    monkeypatch.setattr(store, "LEASE", timedelta(seconds=300))
    takeover = store.claim("c", "takeover", principal_id=claimant["id"], principal_name=claimant["name"])
    assert takeover.took_over
    assert not finish(stalled, rid, [{"type": "answer"}])
    assert finish(takeover, rid, [{"type": "answer"}])
    assert last_seq() == 6


@pytest.mark.parametrize("table", ["request_message", "request_event"])
def test_direct_insert_without_seq_is_refused(engine, table):
    _, rid = start(engine)
    with sqlite3.connect(store.path()) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="seq"):
            if table == "request_message":
                conn.execute("INSERT INTO request_message (conversation_id, turn_id, text, at) VALUES ('c', 't', '', '')")
            else:
                conn.execute("INSERT INTO request_event (request_id, conversation_id, turn_id, event, at) "
                             "VALUES (?, 'c', 't', '{}', '')", (rid,))
    assert last_seq() == 1


def test_event_conversation_must_match_its_request(engine):
    _, rid = start(engine)
    _, other = start(engine, "other")
    with sqlite3.connect(store.path()) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="conversation"):
            conn.execute("INSERT INTO request_event (request_id, conversation_id, turn_id, event, at, seq) "
                         "VALUES (?, 'other', 't', '{}', '', 2)", (rid,))
    store.add_events(rid, [{"type": "notice"}])
    with sqlite3.connect(store.path()) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE request_event SET conversation_id = 'other'")
        with pytest.raises(sqlite3.IntegrityError, match="conversation"):
            conn.execute("UPDATE request_event SET request_id = ?", (other,))
        with pytest.raises(sqlite3.IntegrityError, match="conversation"):
            conn.execute("UPDATE request SET conversation_id = 'other' WHERE id = ?", (rid,))


def test_seq_is_unique_across_tables_and_immutable(engine):
    _, rid = start(engine)
    with sqlite3.connect(store.path()) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO request_event (request_id, conversation_id, turn_id, event, at, seq) "
                         "VALUES (?, 'c', 't', '{}', '', 1)", (rid,))
    store.add_events(rid, [{"type": "notice"}])
    with sqlite3.connect(store.path()) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO request_message (conversation_id, turn_id, text, at, seq) "
                         "VALUES ('c', 't', '', '', 2)")
        for table in ("request_message", "request_event"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(f"UPDATE {table} SET seq = 3")
    assert last_seq() == 2


def test_two_writers_serialize_and_conversations_have_independent_sequences(engine):
    _, rid = start(engine)
    _, other = start(engine, "other")
    barrier = threading.Barrier(2)

    def write():
        barrier.wait(timeout=10)
        for n in range(25):
            store.add_events(rid, [{"type": "notice", "n": n}])
            store.add_events(other, [{"type": "notice", "n": n}])

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write) for _ in range(2)]
        for future in futures:
            future.result(timeout=30)
    for cid in ("c", "other"):
        page = store.history_page(cid)
        assert [x["seq"] for x in page["items"]] == list(range(1, 52))
        assert last_seq(cid) == 51


def test_writer_holds_sqlite_write_lock_until_counter_and_row_commit(engine, monkeypatch):
    _, rid = start(engine)
    real_open = store._open
    inside = threading.Event()
    second_started = threading.Event()
    second_acquired = threading.Event()
    allow_commit = threading.Event()

    def opened():
        conn = real_open()
        name = threading.current_thread().name

        def trace(sql):
            if name == "first" and sql.startswith("INSERT INTO request_event"):
                inside.set()
                assert allow_commit.wait(timeout=10)
            if name == "second" and sql.startswith("BEGIN"):
                second_started.set()
            if name == "second" and (sql.startswith("UPDATE conversation") or sql.startswith("SELECT")):
                second_acquired.set()
        conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(store, "_open", opened)
    errors = []

    def write():
        try:
            store.add_events(rid, [{"type": "notice"}])
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=write, name="first")
    second = threading.Thread(target=write, name="second")
    first.start()
    try:
        assert inside.wait(timeout=10)
        second.start()
        assert second_started.wait(timeout=10)
        assert not second_acquired.wait(timeout=0.1)
    finally:
        allow_commit.set()
        first.join(timeout=10)
        if second.ident is not None:
            second.join(timeout=10)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert rows("SELECT seq FROM request_event ORDER BY id") == [(2,), (3,)]


def legacy_fixture(version=3, neighbours=False, partial=False):
    """A real pre-seq store, including historical bad dates and unassigned messages."""
    p = store.path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(p) as conn:
        conn.execute("CREATE TABLE store_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO store_version VALUES (?)", (version,))
        for ddl in store._V1 + store._V2 + (store._V3 if version == 3 else []):
            # Old/imported rows can have NULL dates even though today's DDL refuses them.
            conn.execute(ddl.replace("at TEXT NOT NULL", "at TEXT"))
        conn.execute("INSERT INTO conversation (id) VALUES ('c')")
        conn.execute("INSERT INTO conversation (id) VALUES ('empty')")
        conn.execute("INSERT INTO request (id, conversation_id, token, state, created_at) "
                     "VALUES ('r', 'c', 'token', 'open', 'original')")
        if neighbours:
            conn.execute("ALTER TABLE conversation ADD COLUMN owner_id TEXT")
            conn.execute("ALTER TABLE request_message ADD COLUMN author TEXT")
            conn.execute("ALTER TABLE request_message ADD COLUMN client_message_id TEXT")
            conn.execute("ALTER TABLE request_message ADD COLUMN outcome TEXT")
        for ident, at in [(1, '2026-01-01T00:00:00Z'), (2, None), (3, 'bad date'),
                          (4, '2026-01-01T00:00:00Z'), (5, '2025-01-01T00:00:00Z')]:
            conn.execute("INSERT INTO request_message (id, conversation_id, request_id, turn_id, text, choice, at) "
                         "VALUES (?, 'c', ?, 't', ?, ?, ?)",
                         (ident, None if ident == 5 else 'r', f'raw \\n {ident} café', ' {"pick": 1} ', at))
        for ident, at in [(1, '2026-01-01T00:00:00Z'), (2, None), (3, '!invalid'),
                          (4, '2025-01-01T00:00:00Z')]:
            conn.execute("INSERT INTO request_event (id, request_id, turn_id, event, at) VALUES (?, 'r', 't', ?, ?)",
                         (ident, ' { "type" : "notice", "text" : "é" } ', at))
        if version == 3:
            conn.execute("INSERT INTO message_resolution (message_id, completed, field) VALUES (1, 1, 'customer')")
            conn.execute("INSERT INTO message_name_span (id, message_id, start, end, kind, record_id, name, area) "
                         "VALUES ('span', 1, 0, 3, 'party', 'party-id', 'VAT House', 'tax')")
            conn.execute("INSERT INTO span_confirmation (span_id, question, classification) "
                         "VALUES ('span', ' {\"text\": \"name?\"} ', 'name')")
        if partial:
            conn.execute("ALTER TABLE conversation ADD COLUMN last_seq INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE request_message ADD COLUMN seq INTEGER")
            conn.execute("ALTER TABLE request_event ADD COLUMN seq INTEGER")
            conn.execute("ALTER TABLE request_event ADD COLUMN conversation_id TEXT")
            conn.execute("UPDATE request_message SET seq = 99 WHERE id = 1")
    store._ready.discard(str(p))
    return p


def content_snapshot():
    with sqlite3.connect(store.path()) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        snapshot = {}
        for table in tables:
            if table == "store_version":
                continue
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info({table})')
                    if r[1] not in ("seq", "last_seq") and not (table == "request_event" and r[1] == "conversation_id")]
            snapshot[table] = conn.execute(f'SELECT {", ".join(cols)} FROM {table} ORDER BY rowid').fetchall()
        return snapshot


def assert_backfill():
    expected = [("engine", 2), ("engine", 3), ("owner", 2), ("owner", 3),
                ("engine", 4), ("owner", 5), ("engine", 1), ("owner", 1), ("owner", 4)]
    page = store.history_page("c")
    assert [(x["kind"], x["id"]) for x in page["items"]] == expected
    assert [x["seq"] for x in page["items"]] == list(range(1, 10))
    assert last_seq() == 9
    assert last_seq("empty") == 0
    assert rows("SELECT conversation_id FROM request_event") == [("c",)] * 4


def test_migration_equal_timestamps_order_by_table_name_then_id():
    p = legacy_fixture()
    with sqlite3.connect(p) as conn:
        for table in ("request_message", "request_event"):
            conn.execute(f"UPDATE {table} SET at = '2026-01-01T00:00:00Z'")

    assert store.version() == 5
    page = store.history_page("c")
    assert [(x["seq"], x["kind"], x["id"]) for x in page["items"]] == [
        (1, "engine", 1), (2, "engine", 2), (3, "engine", 3), (4, "engine", 4),
        (5, "owner", 1), (6, "owner", 2), (7, "owner", 3), (8, "owner", 4), (9, "owner", 5),
    ]
    assert page["next"] == last_seq() == 9


def test_migration_backfill_content_and_idempotence(engine):
    with engine.connect() as conn:
        claimant = pr.owner(conn)
    p = legacy_fixture()
    before = content_snapshot()
    before["conversation"] = [row + (None, None) for row in before["conversation"]]
    before["request_message"] = [row + (None, None, None, None) for row in before["request_message"]]
    before["conversation_participant"] = []
    before["store_receipt"] = []
    assert store.version() == 5
    assert_backfill()
    assert content_snapshot() == before
    once = rows("SELECT * FROM request_message"), rows("SELECT * FROM request_event")
    # Actually rerun v4, including its guarded ALTERs, against its own output.
    with sqlite3.connect(p) as conn:
        conn.execute("UPDATE store_version SET version = 3")
    store._ready.discard(str(p))
    assert store.version() == 5
    assert_backfill()
    assert once == (rows("SELECT * FROM request_message"), rows("SELECT * FROM request_event"))
    # Ordinary subsequent opens must not reorder newly accepted rows by their clocks.
    store.assign_legacy_owner(engine, claimant["id"])
    c = store.claim("c", "new", principal_id=claimant["id"], principal_name=claimant["name"])
    with sqlite3.connect(p) as conn:
        conn.execute("UPDATE request_message SET at = '1900-01-01T00:00:00Z' WHERE id = ?", (c.message_id,))
    store._ready.discard(str(p))
    assert store.version() == 5
    assert rows("SELECT seq FROM request_message WHERE id = ?", (c.message_id,)) == [(10,)]


@pytest.mark.parametrize("neighbours", [False, True])
@pytest.mark.parametrize("partial", [False, True])
def test_v2_and_partial_schemas_tolerate_optional_neighbour_columns(engine, neighbours, partial):
    with engine.connect() as conn:
        claimant = pr.owner(conn)
    legacy_fixture(version=2, neighbours=neighbours, partial=partial)
    assert store.version() == 5
    assert_backfill()
    assert all(x["author"] == "owner" for x in store.history_page("c")["items"] if x["kind"] == "owner")
    store.assign_legacy_owner(engine, claimant["id"])
    assert store.claim("c", "after migration", principal_id=claimant["id"], principal_name=claimant["name"])
    assert last_seq() == 10


def test_two_processes_migrate_one_store():
    legacy_fixture(partial=True)
    script = "from erpclaw_assistant import store; assert store.version() == 5"
    procs = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    for proc in procs:
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, (out, err)
    assert_backfill()


def test_killed_mid_backfill_rolls_back_version_and_converges():
    legacy_fixture()
    script = '''
import os
import sqlite3
from erpclaw_assistant import store
real_connect = sqlite3.connect
class Connection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        result = super().execute(sql, parameters)
        if sql.startswith("UPDATE request_message SET seq"):
            os._exit(79)  # a real process death with a partially updated transaction
        return result
sqlite3.connect = lambda *a, **kw: real_connect(*a, **kw, factory=Connection)
store.version()
'''
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=30)
    assert done.returncode == 79, done.stderr
    assert rows("SELECT version FROM store_version") == [(3,)]
    assert "seq" not in [r[1] for r in rows("PRAGMA table_info(request_message)")]
    assert store.version() == 5
    assert_backfill()


def test_timeline_shape_and_pages_follow_seq_despite_clock_steps(engine):
    with engine.connect() as conn:
        claimant = pr.owner(conn)
    c, rid = start(engine)
    store.add_events(rid, [{"type": "notice", "text": "first"}])
    assert finish(c, rid, [{"type": "answer", "text": "second"}])
    c = store.claim("c", "next", principal_id=claimant["id"], principal_name=claimant["name"])
    assert store.assign(c, rid)
    store.add_events(rid, [{"type": "notice", "text": "last"}])
    with sqlite3.connect(store.path()) as conn:
        conn.execute("UPDATE request_message SET at = '2099-01-01T00:00:00Z' WHERE seq = 1")
        conn.execute("UPDATE request_event SET at = '1900-01-01T00:00:00Z' WHERE seq = 5")
    timeline = store.timeline(rid)
    assert all(len(x) == 3 for x in timeline)
    assert [x[0] for x in timeline] == ["owner", "engine", "engine", "owner", "engine"]
    assert timeline[0][1] == "owner" and timeline[-1][1]["text"] == "last"
    all_items = []
    after = 0
    while True:
        page = store.history_page("c", after=after, limit=2)
        if not page["items"]:
            assert page["next"] == after
            break
        assert page["next"] == page["items"][-1]["seq"]
        all_items.extend(page["items"])
        after = page["next"]
    assert [x["seq"] for x in all_items] == [1, 2, 3, 4, 5]
    assert all("at" in x and "request_id" in x for x in all_items)
    assert store.history_page("missing") == {"items": [], "next": 0}
    assert store.history_page("c", after=3)["items"] == all_items[3:]


def test_page_queries_use_both_conversation_seq_indexes(engine):
    _, rid = start(engine)
    store.add_events(rid, [{"type": "notice"}])
    for table in ("request_message", "request_event"):
        plan = rows(f"EXPLAIN QUERY PLAN SELECT * FROM {table} WHERE conversation_id = ? AND seq > ? "
                    "ORDER BY seq LIMIT ?", ("c", 0, 200))
        assert any("USING INDEX" in r[3] and "conversation_id=? AND seq>?" in r[3] for r in plan), plan
    statements = []
    original = store._open

    def opened():
        conn = original()
        conn.set_trace_callback(statements.append)
        return conn

    from unittest.mock import patch
    with patch.object(store, "_open", opened):
        store.history_page("c", after=1, limit=2)
    for table in ("request_message", "request_event"):
        assert any(f"FROM {table}" in sql and "conversation_id = 'c' AND seq > 1" in sql for sql in statements)


@pytest.mark.parametrize("after,limit", [(-1, 1), (0, 0), (0, 201)])
def test_page_rejects_invalid_bounds(after, limit):
    with pytest.raises(ValueError):
        store.history_page("c", after=after, limit=limit)


def test_failed_batch_rolls_back_rows_counter_and_request_state(engine):
    _, rid = start(engine)
    with sqlite3.connect(store.path()) as conn:
        conn.execute("CREATE TRIGGER fail_second BEFORE INSERT ON request_event "
                     "WHEN NEW.seq = 3 BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        store.end_request(rid, "done", [{"type": "notice"}, {"type": "answer"}])
    assert store.get_request(rid)["state"] == "open"
    assert store.events(rid) == []
    assert last_seq() == 1


@pytest.mark.parametrize("writer", ["claim", "finalise", "reconcile_outcome", "end_request", "set_pending", "add_events"])
def test_each_writer_rolls_back_if_seq_assignment_is_missing(engine, writer, monkeypatch):
    with engine.connect() as conn:
        claimant = pr.owner(conn)
    c, rid = start(engine)
    store.set_pending(rid, card="card")
    before = content_snapshot()
    monkeypatch.setattr(store, "_next_seq", lambda *args: None)
    batch = [{"type": "notice"}]
    with pytest.raises(sqlite3.IntegrityError, match="seq"):
        if writer == "claim":
            store.claim("new", "message", principal_id=claimant["id"], principal_name=claimant["name"])
        elif writer == "finalise":
            finish(c, rid, batch)
        elif writer == "reconcile_outcome":
            store.reconcile_outcome(c, "card", "committed", rid, "ended", events=batch, end_reason="done")
        elif writer == "end_request":
            store.end_request(rid, "done", batch)
        elif writer == "set_pending":
            store.set_pending(rid, question={"text": "pending"}, events=batch)
        else:
            store.add_events(rid, batch)
    assert content_snapshot() == before
    assert last_seq() == 1


def test_pages_resume_across_a_gap(engine):
    _, rid = start(engine)
    with sqlite3.connect(store.path()) as conn:
        conn.execute("UPDATE conversation SET last_seq = 10 WHERE id = 'c'")
    store.add_events(rid, [{"type": "notice"}])
    page = store.history_page("c", after=1)
    assert [x["seq"] for x in page["items"]] == [11]
    assert page["next"] == 11


def test_store_sequence_writes_leave_the_books_untouched(engine):
    from bookdiff import snapshot

    before = snapshot(engine)
    c, rid = start(engine)
    store.add_events(rid, [{"type": "notice"}])
    store.set_pending(rid, card="card", events=[{"type": "ask"}])
    assert store.reconcile_outcome(c, "card", "committed", rid, "continued", events=[{"type": "notice"}]) == "applied"
    assert finish(c, rid, [{"type": "answer"}])
    assert store.end_request(rid, "done", [{"type": "notice"}])
    assert snapshot(engine) == before
