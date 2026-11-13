"""Durable polling, live claim markers, and the store's watched approval ids."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest
from sqlalchemy import MetaData, event, select

from erpclaw_assistant import store
from erpclaw_core import approvals


def start(cid="private", text="hello"):
    c = store.claim(cid, text, principal_id="owner-a", principal_name="Alice")
    assert c is not None
    request = store.new_request(c)
    assert request is not None
    assert store.assign(c, request["id"])
    return c, request["id"]


def store_snapshot():
    with sqlite3.connect(store.path()) as conn:
        return {name: conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
                for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")}


def books_snapshot(engine):
    tables = MetaData()
    tables.reflect(engine)
    with engine.connect() as conn:
        return {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr)
                for table in tables.sorted_tables}


def test_repeated_history_polls_share_durable_order_and_resume_cursor():
    """Catches poll-only filtering, ordering, cursor changes, and non-idempotent reads."""
    c, first = start()
    store.add_events(first, [{"type": "notice", "text": "first event"}])
    store.release(c)
    c = store.claim("private", "follow up", "Q1-2", principal_id="owner-a", principal_name="Alice")
    assert c is not None and store.assign(c, first)
    assert store.finalise(c, first, [{"type": "answer", "text": "finished"}], pending_question=None,
                          pending_card=None, state="ended", why="done", serial_now=0, bindings_now={},
                          intent_state="ended")
    _, second = start(text="a new request")
    assert first != second
    store.add_events(second, [{"type": "card", "approval_id": "card-new"},
                              {"type": "notice", "text": "last event"}])
    _, other = start("other")
    store.add_events(other, [{"type": "notice", "text": "another conversation"}])
    with sqlite3.connect(store.path()) as conn:
        for table in ("request_message", "request_event"):
            conn.execute(f"UPDATE {table} SET at = printf('%04d-01-01T00:00:00Z', 2100 - seq) "
                         "WHERE conversation_id = 'private'")
    before = store_snapshot()
    oracle = store.history_page("private")
    assert [item["seq"] for item in oracle["items"]] == list(range(1, 8))
    streams = [[], []]
    cursors = [0, 0]
    for _ in range(4):
        for index in (0, 1):
            page = store.history_page("private", after=cursors[index], limit=2)
            repeated = store.history_page("private", after=cursors[index], limit=2)
            assert json.dumps(page, ensure_ascii=False).encode() == json.dumps(repeated, ensure_ascii=False).encode()
            assert page["items"] == [item for item in oracle["items"] if item["seq"] > cursors[index]][:2]
            assert page["next"] == (page["items"][-1]["seq"] if page["items"] else cursors[index])
            streams[index].extend(page["items"])
            cursors[index] = page["next"]
    assert streams == [oracle["items"], oracle["items"]]
    assert cursors == [oracle["next"], oracle["next"]]
    assert store.history_page("private", after=cursors[0]) == {"items": [], "next": 7}
    assert store_snapshot() == before


@pytest.mark.parametrize("state", ["missing", "empty", "live", "stalled", "boundary", "expired",
                                    "no_deadline", "no_holder", "released", "renewed", "takeover"])
def test_live_marker_matches_the_existing_lease_without_writes(monkeypatch, state):
    """Catches absent readers, false live markers, changed expiry rules, and stored working events."""
    now = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(store, "_now", lambda: now)
    store.create_conversation("private", "owner-a")
    c = None
    cid = "unknown" if state == "missing" else "private"
    if state not in {"missing", "empty"}:
        c, _ = start()
        if state == "released":
            store.release(c)
        elif state == "renewed":
            now += timedelta(seconds=30)
            assert store.renew(c)
        elif state == "takeover":
            now += store.LEASE
            replacement = store.claim("private", "replacement", principal_id="owner-a", principal_name="Alice")
            assert replacement is not None and replacement.took_over
            with store._tx() as conn:
                assert not store._holds(conn, c)
            c = replacement
        elif state in {"boundary", "expired"}:
            now += store.LEASE + timedelta(microseconds=1 if state == "expired" else 0)
        elif state == "stalled":
            now += store.STALLED_AFTER + timedelta(seconds=1)
            assert store.holder_stalled(cid)
        elif state == "no_deadline":
            with sqlite3.connect(store.path()) as conn:
                conn.execute("UPDATE conversation SET claim_until = NULL WHERE id = ?", (cid,))
        elif state == "no_holder":
            with sqlite3.connect(store.path()) as conn:
                conn.execute("UPDATE conversation SET claimed_by = NULL WHERE id = ?", (cid,))
    with sqlite3.connect(store.path()) as conn:
        row = conn.execute("SELECT claimed_by, claim_until FROM conversation WHERE id = ?", (cid,)).fetchone()
    live = state in {"live", "stalled", "renewed", "takeover"}
    if c is not None:
        with store._tx() as conn:
            assert store._holds(conn, c) is live
    if state != "no_deadline":
        assert store.claim_lapsed(cid) is (not live)
    before, history = store_snapshot(), store.history_page(cid)
    statements = []
    original = store._open

    def opened():
        conn = original()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(store, "_open", opened)
    marker = store.live_turn(cid)
    expected = None if row is None else {"claimed_by": row[0], "claim_until": row[1],
                                         "turn_id": row[0] if live else None, "live": live}
    assert marker == expected
    assert store.live_turn(cid) == expected
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")) for sql in statements)
    assert all("FROM conversation " in sql for sql in statements if sql.startswith("SELECT"))
    assert sum(sql.startswith("BEGIN") for sql in statements) == 2
    assert store_snapshot() == before
    assert store.history_page(cid) == history
    assert store.history_page(cid, after=history["next"]) == {"items": [], "next": history["next"]}


@pytest.mark.parametrize("ending", ["end_request", "reconcile_outcome"])
def test_watched_ids_include_announced_ended_cards_and_only_this_conversation(engine, monkeypatch, ending):
    """Catches open-only/ended-column reads, missing deduplication, foreign ids, statuses, and writes."""
    c, ended = start()
    store.set_pending(ended, card="ended-card", events=[{"type": "card", "approval_id": "ended-card"}])
    if ending == "end_request":
        assert store.end_request(ended, "done")
    else:
        assert store.reconcile_outcome(c, "ended-card", "committed", ended, "ended", end_reason="done") == "applied"
    assert store.get_request(ended)["pending_card"] is None
    store.release(c)
    c, opened = start(text="next request")
    store.set_pending(opened, card="z-open-only")
    cards = [{"type": "card", "approval_id": "a-superseded"},
             {"type": "card", "approval_id": "m-announced"},
             {"type": "card", "approval_id": "ended-card"},
             {"type": "card", "approval_id": "m-announced"}]
    store.add_events(opened, cards + [{"type": "notice", "approval_id": "not-a-card"},
                                     {"type": "answer", "approval_id": "also-not-a-card"}])
    _, foreign = start("foreign")
    store.set_pending(foreign, card="foreign-pending", events=[{"type": "card", "approval_id": "foreign-event"}])
    store.create_conversation("empty", "owner-a")
    before_store, before_books = store_snapshot(), books_snapshot(engine)
    statements = []
    original = store._open

    def opened_store():
        conn = original()
        conn.set_trace_callback(statements.append)
        return conn

    def forbidden(*args, **kwargs):
        pytest.fail("watching ids must not read card status or books")

    monkeypatch.setattr(store, "_open", opened_store)
    monkeypatch.setattr(approvals, "_status_now", forbidden)
    monkeypatch.setattr(approvals, "_card", forbidden)
    event.listen(engine, "before_cursor_execute", forbidden)
    try:
        expected = ["a-superseded", "ended-card", "m-announced", "z-open-only"]
        assert store.watched_card_ids("private") == expected
        assert store.watched_card_ids("private") == expected
        assert store.watched_card_ids("foreign") == ["foreign-event", "foreign-pending"]
        assert store.watched_card_ids("empty") == []
        assert store.watched_card_ids("unknown") == []
    finally:
        event.remove(engine, "before_cursor_execute", forbidden)
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")) for sql in statements)
    assert sum(sql.startswith("BEGIN") for sql in statements) == 5
    assert all("FROM request " in sql or "FROM request_event " in sql
               for sql in statements if sql.startswith("SELECT"))
    assert store_snapshot() == before_store
    assert books_snapshot(engine) == before_books


@pytest.mark.parametrize("with_durable", [False, True])
def test_terminal_decision_is_never_stored_or_replayed(with_durable):
    """Catches decisions stored with a seq or transient-only counter increments."""
    _, rid = start()
    store.set_pending(rid, card="ended-card", events=[{"type": "card", "approval_id": "ended-card"}])
    before = store.history_page("private")
    decisions = [{"type": "decision", "approval_id": "ended-card", "status": status, "at": "2026-11-08T12:00:00Z"}
                 for status in ("committed", "declined", "expired", "invalidated", "superseded", "refused")]
    durable = [{"type": "notice", "text": "request ended"}] if with_durable else []
    assert store.end_request(rid, "done", decisions + durable)
    assert store.get_request(rid)["pending_card"] is None
    page = store.history_page("private", after=before["next"])
    assert [item["event"] for item in page["items"]] == durable
    assert page["next"] == before["next"] + len(durable)
    assert store.history_page("private", after=before["next"]) == page
    assert store.events(rid) == [{"type": "card", "approval_id": "ended-card"}] + durable
    assert store.history_page("private")["items"] == before["items"] + page["items"]
    with sqlite3.connect(store.path()) as conn:
        assert conn.execute("SELECT last_seq FROM conversation WHERE id = 'private'").fetchone() == (page["next"],)


@pytest.mark.parametrize("cid,path", [("private", "poll"), ("private", "markers"), ("unknown", "all")])
def test_ownership_precheck_touches_no_protected_stream_data(monkeypatch, cid, path):
    """Catches polling history, live claims, or watched ids before a failed ownership check."""
    _, rid = start()
    store.set_pending(rid, card="secret-card", events=[{"type": "card", "approval_id": "secret-card"}])
    assert store.conversation_owner("private") == "owner-a"
    assert store.conversation_owner("unknown") is None
    calls = Counter(history=0, live=0, watched=0)
    selects = Counter(request_message=0, request_event=0)
    original = store._open

    def trace(sql):
        for table in selects:
            if sql.startswith("SELECT") and f"FROM {table} " in sql:
                selects[table] += 1

    def opened():
        conn = original()
        conn.set_trace_callback(trace)
        return conn

    def counted_history(*args, **kwargs):
        calls["history"] += 1
        return history(*args, **kwargs)

    def counted_live(*args, **kwargs):
        calls["live"] += 1
        return live(*args, **kwargs)

    def counted_watched(*args, **kwargs):
        calls["watched"] += 1
        return watched(*args, **kwargs)

    history, live, watched = store.history_page, store.live_turn, store.watched_card_ids
    monkeypatch.setattr(store, "_open", opened)
    monkeypatch.setattr(store, "history_page", counted_history)
    monkeypatch.setattr(store, "live_turn", counted_live)
    monkeypatch.setattr(store, "watched_card_ids", counted_watched)
    result = None
    if store.conversation_owner(cid) == "reader-b":
        result = {}
        if path in {"poll", "all"}:
            result["history"] = store.history_page(cid)
        if path in {"markers", "all"}:
            result["live"] = store.live_turn(cid)
            result["watched"] = store.watched_card_ids(cid)
    assert calls == Counter(history=0, live=0, watched=0)
    assert selects == Counter(request_message=0, request_event=0)
    assert result is None
    if store.conversation_owner("private") == "owner-a":
        assert len(store.history_page("private")["items"]) == 2
        assert store.live_turn("private")["live"] is True
        assert store.watched_card_ids("private") == ["secret-card"]
    assert calls == Counter(history=1, live=1, watched=1)
    assert selects["request_message"] == 1
    assert selects["request_event"] >= 2
