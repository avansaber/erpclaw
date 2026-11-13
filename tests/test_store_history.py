"""History item fields, durable cursors, and the ownership pre-check contract."""
import sqlite3

import pytest

from erpclaw_assistant import store


def claim_message(text="hello", choice=None, name="Alice"):
    c = store.claim("private", text, choice, principal_id="owner-a", principal_name=name)
    assert c is not None
    return c


@pytest.fixture
def started():
    c = claim_message()
    request = store.new_request(c)
    assert request is not None
    assert store.assign(c, request["id"])
    return c, request["id"]


@pytest.fixture
def five_items(started):
    c, rid = started
    store.add_events(rid, [{"type": "notice", "text": "first"}, {"type": "answer", "text": "second"}])
    store.release(c)
    second = claim_message("next")
    request = store.new_request(second)
    assert request is not None
    assert store.assign(second, request["id"])
    store.add_events(request["id"], [{"type": "ask", "text": "last"}])
    return "private"


@pytest.mark.parametrize("part", ["choices", "authors", "events"])
def test_history_preserves_exact_owner_and_engine_shapes(started, part):
    c, rid = started
    store.release(c)
    for text, choice, name in [("  yes, proceed\n漢字  ", " Q7-2 ", "  Approve as Bob  "),
                               ("empty choice", "", ""), ("legacy", None, "old name")]:
        c = claim_message(text, choice if part == "choices" else None, name)
        store.release(c)
    with sqlite3.connect(store.path()) as conn:
        conn.execute("UPDATE request_message SET author_name = NULL, author_principal_id = NULL WHERE id = ?",
                     (c.message_id,))
        conn.row_factory = sqlite3.Row
        messages = conn.execute("SELECT * FROM request_message ORDER BY seq").fetchall()
    events = [{"type": "notice", "text": "  café\n漢字  ", "extra": {"list": [None, False, 3.5]}},
              {"type": "ask", "question": {"choices": [" yes ", "no"]}, "optional": None},
              {"type": "card", "approval_id": "card-private", "unknown": [1, {"raw": "\t"}]}]
    store.add_events(rid, events)
    page = store.history_page("private")
    owners = [item for item in page["items"] if item["kind"] == "owner"]
    expected = []
    for row in messages:
        item = {"kind": "owner", "id": row["id"], "request_id": row["request_id"], "seq": row["seq"],
                "at": row["at"], "text": row["text"],
                "author": row["author_name"] if row["author_name"] is not None else "owner"}
        if row["choice"] is not None:
            item["choice"] = row["choice"]
        expected.append(item)
    if part != "events":
        assert owners == expected
    with sqlite3.connect(store.path()) as conn:
        stored = conn.execute("SELECT id, request_id, seq, at FROM request_event ORDER BY seq").fetchall()
    engines = [item for item in page["items"] if item["kind"] == "engine"]
    assert engines == [{"kind": "engine", "id": row[0], "request_id": row[1], "seq": row[2],
                        "at": row[3], "event": event} for row, event in zip(stored, events, strict=True)]
    assert page["next"] == 7


def test_history_pages_follow_seq_with_original_clock_values(five_items):
    stamps = {1: "2099-01-01T00:00:00Z", 2: "2050-01-01T00:00:00Z", 3: "2000-01-01T00:00:00Z",
              4: "1950-01-01T00:00:00Z", 5: "1900-01-01T00:00:00Z"}
    with sqlite3.connect(store.path()) as conn:
        for seq, at in stamps.items():
            for table in ("request_message", "request_event"):
                conn.execute(f"UPDATE {table} SET at = ? WHERE seq = ?", (at, seq))
    items = []
    after = 0
    for _ in range(3):
        page = store.history_page(five_items, after=after, limit=2)
        items.extend(page["items"])
        after = page["next"]
    assert [(item["seq"], item["at"]) for item in items] == list(stamps.items())


def test_history_cursor_pages_each_item_once_and_keeps_empty_cursor(five_items):
    after = 0
    for expected in ([1, 2], [3, 4], [5]):
        page = store.history_page(five_items, after=after, limit=2)
        assert [item["seq"] for item in page["items"]] == expected
        assert page["next"] == expected[-1]
        after = page["next"]
    assert store.history_page(five_items, after=after, limit=2) == {"items": [], "next": 5}
    assert [item["seq"] for item in store.history_page(five_items, after=3)["items"]] == [4, 5]
    assert store.history_page("unknown") == {"items": [], "next": 0}
    assert store.history_page("unknown", after=17) == {"items": [], "next": 17}


def test_history_resumes_inside_and_after_a_sequence_gap(started):
    _, rid = started
    with sqlite3.connect(store.path()) as conn:
        conn.execute("UPDATE conversation SET last_seq = 7 WHERE id = 'private'")
    store.add_events(rid, [{"type": "notice", "text": "after gap"}, {"type": "answer", "text": "tail"}])
    for after in (1, 4, 7):
        page = store.history_page("private", after=after, limit=1)
        assert [item["seq"] for item in page["items"]] == [8]
        assert page["next"] == 8
    page = store.history_page("private", after=8, limit=1)
    assert [item["seq"] for item in page["items"]] == [9]
    assert page["next"] == 9
    assert store.history_page("private", after=9) == {"items": [], "next": 9}


@pytest.mark.parametrize("writer", ["finalise", "reconcile_outcome", "end_request", "set_pending", "add_events"])
@pytest.mark.parametrize("durable", [False, True])
def test_history_and_counter_exclude_transients_from_every_writer(started, writer, durable):
    c, rid = started
    transients = [{"type": kind, "text": "live", "approval_id": "private-card"}
                  for kind in ("working", "decision", "delivery")]
    events = [{"type": "notice", "text": "durable"}] if durable else []
    batch = transients + events + transients
    if writer == "finalise":
        assert store.finalise(c, rid, batch, pending_question=None, pending_card=None, state="open", why=None,
                              serial_now=0, bindings_now={}, intent_state="unused")
    elif writer == "reconcile_outcome":
        store.set_pending(rid, card="private-card")
        assert store.reconcile_outcome(c, "private-card", "committed", rid, "ended", events=batch) == "applied"
    elif writer == "end_request":
        assert store.end_request(rid, "done", batch)
    elif writer == "set_pending":
        store.set_pending(rid, events=batch)
    else:
        store.add_events(rid, batch)
    assert store.events(rid) == events
    page = store.history_page("private", after=1)
    assert [item["event"] for item in page["items"]] == events
    assert page["next"] == 1 + len(events)
    with sqlite3.connect(store.path()) as conn:
        assert conn.execute("SELECT last_seq FROM conversation WHERE id = 'private'").fetchone() == (1 + len(events),)


def test_conversation_owner_reads_only_the_primary_key_row(started, monkeypatch):
    statements = []
    original = store._open

    def opened():
        conn = original()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(store, "_open", opened)
    assert store.conversation_owner("private") == "owner-a"
    assert store.conversation_owner("unknown") is None
    selects = [sql for sql in statements if sql.startswith("SELECT")]
    assert selects == ["SELECT owner_principal_id FROM conversation WHERE id = 'private'",
                       "SELECT owner_principal_id FROM conversation WHERE id = 'unknown'"]
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
    with sqlite3.connect(store.path()) as conn:
        plan = conn.execute("EXPLAIN QUERY PLAN SELECT owner_principal_id FROM conversation WHERE id = ?",
                            ("private",)).fetchall()
    assert any("USING INDEX sqlite_autoindex_conversation_1 (id=?)" in row[3] for row in plan)


@pytest.mark.parametrize("cid", ["private", "unknown"])
def test_failed_ownership_precheck_performs_zero_protected_selects(started, monkeypatch, cid):
    _, rid = started
    store.add_events(rid, [{"type": "notice", "text": "secret"}])
    reads = {"request_message": 0, "request_event": 0}
    original = store._open

    def trace(sql):
        for table in reads:
            if sql.startswith("SELECT") and f"FROM {table} " in sql:
                reads[table] += 1

    def opened():
        conn = original()
        conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(store, "_open", opened)
    page = None
    if store.conversation_owner(cid) == "reader-b":
        page = store.history_page(cid)
    assert reads == {"request_message": 0, "request_event": 0}
    assert page is None
    if store.conversation_owner("private") == "owner-a":
        page = store.history_page("private")
    assert page is not None and len(page["items"]) == 2
    assert reads == {"request_message": 1, "request_event": 1}


@pytest.mark.parametrize("after,limit", [(-1, 1), (0, 0), (0, 201), ("1", 1), (None, 1), (1.5, 1),
                                        (True, 1), (0, "2"), (0, None), (0, 2.5), (0, True)])
def test_history_invalid_bounds_have_the_fixed_data_free_error(after, limit):
    with pytest.raises(ValueError) as exc:
        store.history_page("private-secret", after=after, limit=limit)
    assert str(exc.value) == "after must be nonnegative and limit must be between 1 and 200"
