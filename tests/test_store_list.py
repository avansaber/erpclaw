"""Owned conversation summaries, stable pagination, and verbatim previews."""
from datetime import datetime, timezone
import sqlite3

import pytest

from erpclaw_assistant import store


BOUNDS_ERROR = "after must be a (last_at, id) string pair or None and limit must be between 1 and 50"
FIELDS = {"id", "started_at", "last_at", "state", "pending_card", "messages", "first_message"}


def claim(cid, text="hello", owner="owner-a"):
    c = store.claim(cid, text, principal_id=owner, principal_name="Display name")
    assert c is not None
    return c


def start(cid, text="hello", owner="owner-a"):
    c = claim(cid, text, owner)
    req = store.new_request(c)
    assert req is not None
    assert store.assign(c, req["id"])
    return c, req["id"]


def stamp(day):
    return f"2026-01-{day:02d}T00:00:00.000000Z"


def at_day(monkeypatch, day):
    monkeypatch.setattr(store, "_now", lambda: datetime(2026, 1, day, tzinfo=timezone.utc))


def seeded_order(monkeypatch, count=5):
    # Insert tied ids in reverse order to expose a missing ascending tie-break.
    for cid, day in [("z", 4), ("a", 4), ("newest", 5), ("older", 3), ("oldest", 2)][:count]:
        at_day(monkeypatch, day)
        c = claim(cid)
        store.release(c)


@pytest.fixture
def owned_rows():
    c, opened = start("open", "first open words")
    store.set_pending(opened, card="approval-open", events=[{"type": "card", "approval_id": "approval-open"}])
    store.release(c)
    c, ended = start("ended", "first ended words")
    store.set_pending(ended, card="approval-ended")
    assert store.end_request(ended, "done", [{"type": "answer", "text": "finished"}])
    store.release(c)
    c, foreign = start("foreign", "private foreign words", "owner-b")
    store.add_events(foreign, [{"type": "notice"}])
    store.release(c)
    return opened, ended


def test_exact_summary_shape_and_request_state(owned_rows):
    """Mutations: add/drop fields, null timestamps/preview, or retain an ended card."""
    page = store.list_conversations("owner-a")
    assert set(page) == {"conversations", "next"}
    assert page["next"] is None
    by_id = {r["id"]: r for r in page["conversations"]}
    assert set(by_id) == {"open", "ended"}
    for row in by_id.values():
        assert set(row) == FIELDS
        assert isinstance(row["started_at"], str)
        assert isinstance(row["last_at"], str)
        assert isinstance(row["first_message"], str)
        assert row["messages"] >= 1
    assert by_id["open"]["state"] == "open"
    assert by_id["open"]["pending_card"] == store.open_request("open")["pending_card"] == "approval-open"
    assert by_id["ended"]["state"] == "ended"
    assert by_id["ended"]["pending_card"] is None


def test_owner_filter_is_applied_in_sql(owned_rows, monkeypatch):
    """Mutation: remove the SQL owner filter, including post-filtering foreign rows."""
    statements = []
    real_open = store._open

    def opened():
        conn = real_open()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(store, "_open", opened)
    a = store.list_conversations("owner-a")
    assert {r["id"] for r in a["conversations"]} == {"open", "ended"}
    assert any("owner_principal_id = 'owner-a'" in sql for sql in statements)
    assert not any("'foreign'" in sql for sql in statements)
    b = store.list_conversations("owner-b")
    assert [r["id"] for r in b["conversations"]] == ["foreign"]
    assert b["conversations"][0]["messages"] == 1
    assert store.list_conversations("nobody") == {"conversations": [], "next": None}


def test_order_is_latest_timestamp_then_ascending_id(monkeypatch):
    """Mutations: order by id/seq, omit the id tie-break, or reverse it."""
    seeded_order(monkeypatch)
    expected = ["newest", "a", "z", "older", "oldest"]
    for _ in range(3):
        rows = store.list_conversations("owner-a")["conversations"]
        assert [r["id"] for r in rows] == expected
        assert [r["last_at"] for r in rows] == [stamp(d) for d in [5, 4, 4, 3, 2]]


def test_cursor_straddles_timestamp_tie_and_exhausts(monkeypatch):
    """Mutations: timestamp-only cursor, offsets, or a non-strict cursor comparison."""
    seeded_order(monkeypatch)
    expected = store.list_conversations("owner-a")["conversations"]
    gathered = []
    after = None
    for size in (2, 2, 1):
        page = store.list_conversations("owner-a", after=after, limit=2)
        assert len(page["conversations"]) == size
        gathered.extend(page["conversations"])
        last = page["conversations"][-1]
        pair = (last["last_at"], last["id"])
        assert page["next"] == (pair if size == 2 else None)
        after = pair
    assert gathered == expected
    assert len({r["id"] for r in gathered}) == 5
    assert store.list_conversations("owner-a", after=after, limit=2) == {"conversations": [], "next": None}
    # A cursor is an ordering boundary; it need not name an existing row.
    assert [r["id"] for r in store.list_conversations("owner-a", after=(stamp(4), "m"))["conversations"]] == [
        "z", "older", "oldest"]


def test_full_final_page_has_no_next_cursor(monkeypatch):
    """Mutation: set next whenever the returned page is exactly limit rows."""
    seeded_order(monkeypatch, count=4)
    first = store.list_conversations("owner-a", limit=2)
    assert len(first["conversations"]) == 2
    assert first["next"] == (stamp(4), "a")
    final = store.list_conversations("owner-a", after=first["next"], limit=2)
    assert [r["id"] for r in final["conversations"]] == ["z", "older"]
    assert final["next"] is None


@pytest.mark.parametrize("limit", [0, 51, -1, 1.5, "approval-secret", None, True, False])
def test_invalid_limits_have_fixed_error(limit):
    """Mutations: allow out-of-range/non-int limits or echo an offending value."""
    with pytest.raises(ValueError) as exc:
        store.list_conversations("private-owner", limit=limit)
    assert str(exc.value) == BOUNDS_ERROR


def test_limit_fifty_is_allowed_and_default_is_fifty():
    """Mutations: reject 50 or fail to cap the default page at 50."""
    for n in range(51):
        c = claim(f"conversation-{n:02d}")
        store.release(c)
    explicit = store.list_conversations("owner-a", limit=50)
    assert len(explicit["conversations"]) == 50
    assert explicit["next"] is not None
    assert store.list_conversations("owner-a") == explicit


@pytest.mark.parametrize("after", ["private-conversation", (), ("secret-text",),
                                   ("secret-text", "secret-card", "Display name"),
                                   ["secret-text", "secret-card"], (None, "secret-card"),
                                   ("secret-text", 1), 7, {"last_at": "secret-text"}])
def test_invalid_cursors_have_fixed_error(after):
    """Mutations: accept malformed cursor shapes/types or echo business data."""
    with pytest.raises(ValueError) as exc:
        store.list_conversations("private-owner", after=after)
    assert str(exc.value) == BOUNDS_ERROR


def test_messages_count_excludes_engine_events():
    """Mutation: count events as messages (eight instead of three)."""
    c, rid = start("counted")
    store.add_events(rid, [{"type": "notice", "n": n} for n in range(5)])
    store.release(c)
    for text in ("second", "third"):
        c = claim("counted", text)
        assert store.assign(c, rid)
        store.release(c)
    row = store.list_conversations("owner-a")["conversations"][0]
    assert row["messages"] == 3


@pytest.mark.parametrize("text,expected", [
    ("a" * 40, "a" * 40),
    ("b" * 120, "b" * 120),
    (("ab " * 67)[:200], (("ab " * 67)[:200])[:119]),
    ("x" * 200, "x" * 120),
    (("line1\nline2\n" * 17)[:200], (("line1\nline2\n" * 17)[:200])[:119]),
    ("界😀" * 100, "界😀" * 60),
    ("\t  yes, proceed\r\napprove  ", "\t  yes, proceed\r\napprove  "),
    ("a" * 118 + "\t" + "b" * 81, "a" * 118),
    ("a" * 118 + "\r" + "b" * 81, "a" * 118),
    ("a" * 118 + "\u2003" + "b" * 81, "a" * 118),
    ("a" * 120 + " " + "tail", "a" * 120),
], ids=["short", "exact", "words", "long-word", "multiline", "unicode", "verbatim-instructions",
        "tab", "carriage-return", "unicode-whitespace", "boundary-outside-cut"])
def test_preview_cut_preserves_text_and_state(text, expected):
    """Mutations: no cut, mid-word/byte cut, normalize whitespace, or change stored text/state."""
    c = claim("preview", text)
    row = store.list_conversations("owner-a")["conversations"][0]
    assert row["first_message"] == expected
    assert len(row["first_message"]) <= 120
    assert text.startswith(row["first_message"])
    assert store.history_page("preview")["items"][0]["text"] == text
    assert row["state"] == "none" and row["pending_card"] is None
    assert store.open_request("preview") is None
    store.release(c)


def test_empty_conversation_is_hidden_until_first_message():
    """Mutation: include empty owned rows with missing timestamps/preview or zero messages."""
    store.create_conversation("empty", "owner-a")
    c = claim("nonempty")
    store.release(c)
    assert store.history_page("empty", after=7) == {"items": [], "next": 7}
    assert [r["id"] for r in store.list_conversations("owner-a")["conversations"]] == ["nonempty"]
    c = claim("empty", "now present")
    rows = store.list_conversations("owner-a")["conversations"]
    assert {r["id"] for r in rows} == {"empty", "nonempty"}
    assert all(isinstance(r["started_at"], str) and isinstance(r["last_at"], str) and
               isinstance(r["first_message"], str) and r["messages"] == 1 for r in rows)
    store.release(c)


def test_state_comes_only_from_requests_before_and_after_release():
    """Mutations: report ended for no request, or infer state from claim/message presence."""
    c = claim("state")
    for released in (False, True):
        if released:
            store.release(c)
        assert store.open_request("state") is None
        assert store.last_ended_why("state") is None
        with sqlite3.connect(store.path()) as conn:
            assert conn.execute("SELECT COUNT(*) FROM request WHERE conversation_id = 'state'").fetchone() == (0,)
            assert conn.execute("SELECT request_id FROM request_message WHERE conversation_id = 'state'").fetchall() == [(None,)]
        row = store.list_conversations("owner-a")["conversations"][0]
        assert row["state"] == "none" and row["pending_card"] is None
        assert row["messages"] == 1 and row["first_message"] == "hello"
        assert isinstance(row["started_at"], str) and isinstance(row["last_at"], str)
    c, rid = start("state", "request begins")
    assert store.list_conversations("owner-a")["conversations"][0]["state"] == "open"
    assert store.end_request(rid, "done")
    assert store.list_conversations("owner-a")["conversations"][0]["state"] == "ended"
    # An open request takes precedence over an earlier ended request, even without a card.
    req = store.new_request(c)
    assert req is not None
    row = store.list_conversations("owner-a")["conversations"][0]
    assert row["state"] == "open" and row["pending_card"] is None
    store.release(c)


@pytest.mark.parametrize("latest_kind", ["owner", "engine"])
def test_activity_timestamps_follow_seq_through_backward_clock_steps(monkeypatch, latest_kind):
    """Mutations: MAX(at) for last activity, MIN(at) for start, or message-only last activity."""
    at_day(monkeypatch, 8)
    c, rid = start("stepping", "first by seq")
    store.add_events(rid, [{"type": "notice"}])
    store.release(c)
    at_day(monkeypatch, 5)
    c = claim("steady")
    store.release(c)
    assert store.list_conversations("owner-a")["conversations"][0]["id"] == "stepping"
    at_day(monkeypatch, 2)
    if latest_kind == "owner":
        c = claim("stepping", "later by seq")
        assert store.assign(c, rid)
        store.release(c)
    else:
        store.add_events(rid, [{"type": "answer"}])
    rows = store.list_conversations("owner-a")["conversations"]
    assert [r["id"] for r in rows] == ["steady", "stepping"]
    row = rows[1]
    assert row["started_at"] == stamp(8)
    assert row["first_message"] == "first by seq"
    assert row["last_at"] == stamp(2)
    assert row["messages"] == (2 if latest_kind == "owner" else 1)


def test_list_uses_one_transaction_without_changing_store(owned_rows, monkeypatch):
    """Mutations: write during the reader or open separate transactions for each summary."""
    def snapshot():
        with sqlite3.connect(store.path()) as conn:
            return list(conn.iterdump())

    before = snapshot()
    statements = []
    connections = []
    real_open = store._open

    def opened():
        conn = real_open()
        connections.append(conn)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(store, "_open", opened)
    store.list_conversations("owner-a", limit=1)
    assert snapshot() == before
    assert len(connections) == 1
    assert sum(sql.startswith("BEGIN") for sql in statements) == 1
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER"))
                   for sql in statements)
