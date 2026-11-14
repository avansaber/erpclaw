"""Message identity is checked before a claim can change durable conversation state."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from erpclaw_api.app import create_app
from erpclaw_assistant import resolve, store
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import approvals

from test_conversation_authors import people
from test_conversation_ownership import books_snapshot, sqlite_snapshot
from test_store_seq import finish


@pytest.fixture
def clock(monkeypatch):
    current = [datetime(2026, 11, 8, tzinfo=timezone.utc)]
    monkeypatch.setattr(store, "_now", lambda: current[0])
    return current


def claim(text="hello", choice=None, key="retry", cid="c", **more):
    return store.claim(cid, text, choice, client_message_id=key,
                       principal_id="owner", principal_name="Owner", **more)


def message():
    with store._tx() as conn:
        return dict(conn.execute("SELECT * FROM request_message ORDER BY id LIMIT 1").fetchone())


def conversation(cid="c"):
    with store._tx() as conn:
        return dict(conn.execute("SELECT * FROM conversation WHERE id = ?", (cid,)).fetchone())


def completed(c, events=None):
    req = store.new_request(c)
    assert store.assign(c, req["id"])
    assert finish(c, req["id"], events or [{"type": "answer", "text": "Hello."}])
    return req["id"]


def test_first_arrival_stores_identity_and_optional_ids_remain_independent():
    c = claim(choice="Q1-2")
    row = message()
    assert (row["client_message_id"], row["author_principal_id"], row["author_name"], row["text"], row["choice"],
            row["outcome"], row["turn_id"]) == ("retry", "owner", "Owner", "hello", "Q1-2", None, c.turn_id)
    assert conversation()["generation"] == 1 and conversation()["version"] == 0
    with store._tx() as conn:
        assert conn.execute("SELECT count(*) FROM request_message").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM request_event").fetchone()[0] == 0
    store.release(c)
    for key in (None, None, "retry"):
        other = claim(key=key, cid="other")
        assert isinstance(other, store.Claim)
        store.release(other)
    with store._tx() as conn:
        assert conn.execute("SELECT count(*) FROM request_message").fetchone()[0] == 4
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE request_message SET client_message_id = 'retry' WHERE conversation_id = 'other'")


def test_finished_replay_precedes_claim_and_preserves_every_row(clock):
    c = claim()
    events = [{"type": "answer", "text": "Café ☕"}, {"type": "notice", "text": "Done."}]
    completed(c, events)
    before = sqlite_snapshot()
    replay = claim()
    assert replay.kind == "replay"
    assert json.dumps(replay.events, ensure_ascii=False) == json.dumps(events, ensure_ascii=False)
    assert sqlite_snapshot() == before
    # A different live turn blocks a repeat even when that repeat has durable events.
    held = claim("later", key="later")
    before = sqlite_snapshot()
    assert claim() is None
    assert sqlite_snapshot() == before
    store.release(held)


def test_live_repeat_is_busy_and_does_not_refresh_deadline(clock):
    held = claim()
    clock[0] += timedelta(seconds=10)
    before = sqlite_snapshot()
    assert claim() is None
    assert claim(turn_id=held.turn_id) is None
    assert sqlite_snapshot() == before


@pytest.mark.parametrize("difference", ["text", "choice", "author", "author_and_text"])
def test_reused_identity_is_checked_before_liveness_and_writes_nothing(monkeypatch, difference):
    claim()
    if difference.startswith("author"):
        with store._tx() as conn:
            conn.execute("UPDATE request_message SET author_principal_id = 'another'")
    before = sqlite_snapshot()

    def no_liveness(*args):
        pytest.fail("identity mismatch reached the claim check")

    monkeypatch.setattr(store, "_claimable", no_liveness)
    result = claim(text="different" if difference in ("text", "author_and_text") else "hello",
                   choice="Q1-1" if difference == "choice" else None)
    assert result.kind == "reused"
    assert sqlite_snapshot() == before


def test_busy_first_arrival_leaves_its_id_available(engine):
    c = claim(key="held")
    before = sqlite_snapshot()
    assert claim(key="free") is None
    assert sqlite_snapshot() == before
    store.release(c)
    fresh = claim(key="free")
    assert isinstance(fresh, store.Claim) and not fresh.rerun
    with store._tx() as conn:
        assert conn.execute("SELECT client_message_id FROM request_message ORDER BY id").fetchall()[1][0] == "free"
    client, cid, model = api(engine, [Reply("Hello.")])
    owner = store.conversation_owner(cid)
    holder = store.claim(cid, "held", principal_id=owner, principal_name="Owner")
    before_store, before_books = sqlite_snapshot(), books_snapshot(engine)
    busy = post(client, cid, key="available")
    assert busy.status_code == 200 and busy.json()["events"][0]["code"] == "busy"
    assert sqlite_snapshot() == before_store and books_snapshot(engine) == before_books
    assert not model.seen
    store.release(holder)
    assert post(client, cid, key="available").json()["events"][0]["type"] == "answer"
    assert len(model.seen) == 1


def test_rerun_updates_turn_and_has_a_fresh_deadline_once_per_stop(clock):
    old = claim()
    original = message()
    for _ in range(2):
        clock[0] += store.LEASE + timedelta(seconds=1)
        rerun = claim()
        assert isinstance(rerun, store.Claim) and rerun.rerun and rerun.took_over
        assert rerun.message_id == old.message_id and rerun.generation == old.generation + 1
        assert rerun.turn_id != old.turn_id and message()["turn_id"] == rerun.turn_id
        assert conversation()["turn_deadline_at"] == store._stamp(clock[0] + store.TURN_TIMEOUT)
        assert {k: v for k, v in message().items() if k != "turn_id"} == {
            k: v for k, v in original.items() if k != "turn_id"}
        before = sqlite_snapshot()
        assert claim() is None
        assert sqlite_snapshot() == before
        old = rerun
    events = [{"type": "answer", "text": "Recovered."}]
    completed(old, events)
    before = sqlite_snapshot()
    assert claim().events == events
    assert sqlite_snapshot() == before


@pytest.mark.parametrize("checkpointed", [True, False], ids=["after_checkpoint", "before_checkpoint"])
def test_retry_keeps_its_answered_question_between_consecutive_confirmations(engine, clock, monkeypatch,
                                                                           checkpointed):
    """Mutation: a re-run re-matches the answer against the request's current pending question."""
    invoice = call("create_invoice", customer="Wayne", items=[{"item": "copper", "quantity": 5}])
    a = Assistant(engine, ScriptedModel([Reply("", [invoice]), Reply("", [invoice])]))
    [customer] = a.turn("c", "Invoice Wayne for 5 copper")
    assert customer["type"] == "ask" and customer["code"] == "customer_ambiguous"
    assert "Wayne Enterprises" in customer["options"]
    req = store.open_request("c")
    customer_question = req["pending_question"]
    consumed = []
    answer = resolve.Session.answer

    def count_answer(session, code):
        question = session.pending["id"]
        result = answer(session, code)
        if result:
            consumed.append(question)
        return result

    monkeypatch.setattr(resolve.Session, "answer", count_answer)
    with monkeypatch.context() as interrupted:
        if checkpointed:
            def stop(*args, **kwargs):
                raise RuntimeError("interrupted before finalise")

            interrupted.setattr(store, "finalise", stop)
        else:
            save = store.save_resolution

            def stop(c, request_id, resolution):
                if any(m.get("question_id") == customer_question["id"]
                       for m in resolution.get("owner_messages", [])):
                    raise RuntimeError("interrupted before checkpoint")
                return save(c, request_id, resolution)

            interrupted.setattr(store, "save_resolution", stop)
        with pytest.raises(RuntimeError, match="interrupted before"):
            a.turn("c", "yes", client_message_id="confirmation")

    [_, reply] = store.messages(req["id"])
    saved = store.message_resolution(reply["id"])
    pending = store.get_request(req["id"])["pending_question"]
    assert consumed == [customer_question["id"]]
    if checkpointed:
        assert reply["answer"] == 1 and saved["completed"]
        assert saved["question_id"] == customer_question["id"]
        assert saved["field"] == customer_question["field"]
        assert pending["id"] != customer_question["id"] and pending["field"].endswith("item")
        assert not any(e.get("data", {}).get("question_id") == pending["id"] for e in store.events(req["id"]))
    else:
        assert reply["answer"] == 0 and not saved["completed"]
        assert pending == customer_question
    assert approvals.pending_card(engine, "c", ownership_check=_ownership(engine)) is None

    clock[0] += store.LEASE + timedelta(seconds=1)
    retry = Assistant(engine, ScriptedModel([Reply("", [invoice])]))
    events = retry.turn("c", "yes", client_message_id="confirmation")
    assert [e["type"] for e in events] == ["ask"]
    [item] = events
    assert item["code"] == "item_ambiguous" and "Copper Wire" in item["options"]
    assert item["data"]["field"].endswith("item")
    assert approvals.pending_card(engine, "c", ownership_check=_ownership(engine)) is None
    assert consumed == [customer_question["id"]] * (1 if checkpointed else 2)
    assert store.message_resolution(reply["id"])["question_id"] == customer_question["id"]
    assert store.message_resolution(reply["id"])["field"] == customer_question["field"]
    assert store.bindings(req["id"]) == {customer_question["field"]: "Wayne Enterprises"}
    assert len(store.messages(req["id"])) == 2
    if checkpointed:
        assert store.message_resolution(reply["id"]) == saved
    before = sqlite_snapshot()
    assert retry.turn("c", "yes", client_message_id="confirmation") == events
    assert sqlite_snapshot() == before


def test_newest_includes_answers_and_uses_id_instead_of_timestamp(clock):
    stopped = claim()
    store.release(stopped)
    clock[0] -= timedelta(days=1)
    later = claim("answer", key=None)
    assert store.mark_answer(later)
    store.release(later)
    before = sqlite_snapshot()
    result = claim()
    assert result.kind == "overtaken"
    assert message()["outcome"] == "overtaken"
    after = sqlite_snapshot()
    changed = dict(before)
    rows = list(before["request_message"])
    values = list(rows[0])
    values[list(message()).index("outcome")] = "overtaken"
    rows[0] = tuple(values)
    changed["request_message"] = rows
    assert changed == after
    assert conversation()["claimed_by"] is None
    assert message()["turn_id"] == stopped.turn_id
    assert claim().kind == "overtaken"
    assert sqlite_snapshot() == after


def test_recorded_overtaken_outcome_precedes_event_lookup():
    c = claim()
    completed(c)
    with store._tx() as conn:
        conn.execute("UPDATE request_message SET outcome = 'overtaken'")
    before = sqlite_snapshot()
    assert claim().kind == "overtaken"
    assert sqlite_snapshot() == before


def test_replay_reads_only_this_turns_durable_events():
    c = claim()
    transients = [{"type": kind, "text": "live"} for kind in ("working", "decision", "delivery")]
    durable = [{"type": "answer", "text": "The stored answer."}]
    rid = completed(c, transients + durable + transients)
    store.add_events(rid, [{"type": "notice", "text": "An approval later."}])
    before = sqlite_snapshot()
    assert claim().events == durable
    assert sqlite_snapshot() == before


def test_old_store_adds_only_missing_retry_schema_without_a_version_step():
    c = store.claim("c", "old message", principal_id="owner", principal_name="Owner")
    completed(c)
    with store._tx() as conn:
        for row in conn.execute("PRAGMA index_list(request_message)").fetchall():
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (row["name"],)).fetchone()[0]
            if sql and "client_message_id" in sql:
                conn.execute(f'DROP INDEX "{row["name"]}"')
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(request_message)")}
        for column in ("client_message_id", "outcome"):
            if column in columns:
                conn.execute(f"ALTER TABLE request_message DROP COLUMN {column}")
    before = sqlite_snapshot()
    store._ready.discard(str(store.path()))
    assert store.version() == store.VERSION == 5
    with store._tx() as conn:
        assert conn.execute("SELECT client_message_id, outcome, author_principal_id FROM request_message").fetchall()[0][:] == (
            None, None, "owner")
        indexes = [row for row in conn.execute("SELECT sql FROM sqlite_master WHERE type = 'index'")
                   if row["sql"] and "client_message_id" in row["sql"]]
        assert len(indexes) == 1
        assert "UNIQUE" in indexes[0]["sql"].upper()
        assert "WHERE CLIENT_MESSAGE_ID IS NOT NULL" in indexes[0]["sql"].upper()
    once = sqlite_snapshot()
    assert {k: v for k, v in once.items() if k != "request_message"} == {
        k: v for k, v in before.items() if k != "request_message"}
    assert [row[:len(before["request_message"][0])] for row in once["request_message"]] == before["request_message"]
    store._ready.discard(str(store.path()))
    assert store.version() == 5
    assert sqlite_snapshot() == once


def test_reassign_is_fenced_and_only_moves_the_reruns_message(clock):
    original = claim()
    old = store.new_request(original)
    assert store.assign(original, old["id"])
    clock[0] += store.LEASE + timedelta(seconds=1)
    rerun = claim()
    new = store.new_request(rerun)
    before = sqlite_snapshot()
    assert not store.reassign(original, new["id"])
    assert sqlite_snapshot() == before
    assert store.reassign(rerun, new["id"])
    assert store.messages(old["id"]) == []
    assert [m["id"] for m in store.messages(new["id"])] == [rerun.message_id]
    assert store.intent("c") == (new["id"], "open")


def api(engine, replies=()):
    model = ScriptedModel(list(replies))
    client = TestClient(create_app(engine, model, embedded_owner=True))
    cid = client.post("/v1/conversations").json()["id"]
    return client, cid, model


def post(client, cid, text="hello", key="retry", **more):
    return client.post(f"/v1/conversations/{cid}/messages",
                       json={"text": text, "client_message_id": key, **more})


@pytest.mark.parametrize("recorded", [False, True], ids=["newly_detected", "recorded"])
def test_api_overtaken_message_has_the_exact_refusal(engine, recorded):
    client, cid, _ = api(engine)
    owner = store.conversation_owner(cid)
    stopped = store.claim(cid, "hello", client_message_id="retry", principal_id=owner, principal_name="Owner")
    store.release(stopped)
    later = store.claim(cid, "later", principal_id=owner, principal_name="Owner")
    assert store.mark_answer(later)
    store.release(later)
    if recorded:
        assert store.claim(cid, "hello", client_message_id="retry", principal_id=owner,
                           principal_name="Owner").kind == "overtaken"
    response = post(client, cid)
    assert response.status_code == 200
    [event] = response.json()["events"]
    assert event["code"] == "message_overtaken"
    assert event["text"] == (
        "A later message in this conversation was handled first, so this one was not run. "
        "Send it again if you still want it.")


def test_api_accepts_id_compares_stripped_text_and_replays_identical_bytes(engine):
    client, cid, model = api(engine, [Reply("Hello.")])
    first = post(client, cid, "  hello \n")
    assert first.status_code == 200
    assert first.json()["events"][0]["type"] == "answer"
    assert message()["text"] == "hello"
    before_store, before_books = sqlite_snapshot(), books_snapshot(engine)
    replay = post(client, cid)
    assert replay.status_code == 200 and replay.content == first.content
    assert sqlite_snapshot() == before_store and books_snapshot(engine) == before_books
    assert len(model.seen) == 1


@pytest.mark.parametrize("change", [{"text": "different"}, {"choice": "Q2-1"}, {"author": True}])
def test_api_reused_is_409_without_events_or_writes(engine, change):
    client, cid, model = api(engine, [Reply("Hello.")])
    assert post(client, cid).status_code == 200
    if change.get("author"):
        with store._tx() as conn:
            conn.execute("UPDATE request_message SET author_principal_id = 'another'")
        change = {}
    before_store, before_books = sqlite_snapshot(), books_snapshot(engine)
    result = post(client, cid, **change)
    assert result.status_code == 409
    assert result.json()["code"] == "client_message_id_reused" and "events" not in result.json()
    assert sqlite_snapshot() == before_store and books_snapshot(engine) == before_books
    assert len(model.seen) == 1


def test_api_checks_ownership_before_any_id_lookup_even_with_reused_id(engine, people, monkeypatch):
    owner, other, token = people
    client, cid, _ = api(engine, [Reply("Hello.")])
    assert post(client, cid).status_code == 200
    assert store.conversation_owner(cid) == owner["id"]
    before_store, before_books = sqlite_snapshot(), books_snapshot(engine)
    connect = store._open
    statements = []

    def traced_open():
        conn = connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(store, "_open", traced_open)
    headers = {"Authorization": f"Bearer {token}"}
    body = {"text": "changed", "client_message_id": "retry"}
    unknown = client.post("/v1/conversations/unknown/messages", json=body, headers=headers)
    foreign = client.post(f"/v1/conversations/{cid}/messages", json=body, headers=headers)
    assert unknown.status_code == foreign.status_code == 404
    assert unknown.content == foreign.content == b'{"detail":"That conversation was not found.","code":"no_conversation"}'
    assert unknown.headers == foreign.headers
    assert not any("request_message" in sql.lower() for sql in statements)
    assert sqlite_snapshot() == before_store and books_snapshot(engine) == before_books


def _ownership(engine):
    from erpclaw_assistant import store
    from erpclaw_core import principals

    with engine.connect() as conn:
        owner = principals.owner(conn)["id"]
    return lambda cid: store.conversation_owner(cid) == owner
