"""Claim authority, message authors, and the frozen message refusal."""
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from erpclaw_api.app import create_app
from erpclaw_assistant import store
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel
from erpclaw_core import administration, models as m, principals as pr
from erpclaw_core.common import Refusal

from test_conversation_ownership import books_snapshot, previous_store, sqlite_snapshot


@pytest.fixture
def people(engine):
    with engine.begin() as conn:
        owner = pr.owner(conn)
        company = conn.execute(select(m.company.c.id)).scalar_one()
        token = administration.add_principal(conn, "Alex", m.PERMISSIONS, [company])
        second = pr.authenticate(conn, token)
    return owner, second, token


@pytest.mark.parametrize("held", [False, True])
def test_nonowner_is_refused_before_liveness_and_changes_neither_store_nor_books(engine, people, monkeypatch, held):
    """Catches owner check after liveness, grant overrides, and participant overrides."""
    owner, other, _ = people
    a = Assistant(engine, ScriptedModel([]))
    store.create_conversation("private", owner["id"])
    if held:
        store.claim("private", "first", principal_id=owner["id"], principal_name=owner["name"])
    with sqlite3.connect(store.path()) as conn:
        conn.execute("INSERT INTO conversation_participant VALUES ('private', ?, 'date')", (other["id"],))
    before_store, before_books = sqlite_snapshot(), books_snapshot(engine)

    def no_liveness(*args):
        pytest.fail("a non-owner reached a liveness read")

    monkeypatch.setattr(store, "_claimable", no_liveness)
    monkeypatch.setattr(store, "holder_stalled", no_liveness)
    for create in (False, True):
        with pytest.raises(Refusal) as exc:
            store.claim("private", "approve as owner", principal_id=other["id"], principal_name=other["name"], create=create)
        assert (exc.value.code, exc.value.message) == ("no_conversation", "That conversation was not found.")
    with pytest.raises(Refusal) as exc:
        a.turn("private", "continue", principal=other)
    assert exc.value.code == "no_conversation"
    assert sqlite_snapshot() == before_store
    assert books_snapshot(engine) == before_books


@pytest.mark.parametrize("which", [0, 1])
def test_claim_writes_exact_author_id_and_name_with_text_and_choice(engine, people, which):
    """Catches author columns not written or a caller name replaced by the founding owner's."""
    person = people[which]
    store.create_conversation("mine", person["id"])
    c = store.claim("mine", "hello", "Q7-2", principal_id=person["id"], principal_name=person["name"], create=False)
    assert c is not None
    with sqlite3.connect(store.path()) as conn:
        assert conn.execute("SELECT author_principal_id, author_name, text, choice FROM request_message").fetchall() == [
            (person["id"], person["name"], "hello", "Q7-2")]
        assert conn.execute("SELECT * FROM conversation_participant").fetchall() == []


def test_history_page_reads_nonowner_message_author(engine, people):
    _, other, _ = people
    store.create_conversation("mine", other["id"])
    assert store.claim("mine", "hello", principal_id=other["id"], principal_name=other["name"], create=False)
    page = store.history_page("mine")
    assert [(item["text"], item["author"]) for item in page["items"] if item["kind"] == "owner"] == [
        ("hello", other["name"])]


def test_turn_passes_resolved_principal_id_and_current_name_to_claim(engine, people):
    """Catches missing principal plumbing and trusting a stale caller-supplied name."""
    _, other, _ = people
    a = Assistant(engine, ScriptedModel([Reply("Hello.")]))
    a.turn("direct", "hello", principal={"id": other["id"], "name": "spoofed"})
    with sqlite3.connect(store.path()) as conn:
        assert conn.execute("SELECT owner_principal_id FROM conversation WHERE id = 'direct'").fetchone() == (other["id"],)
        assert conn.execute("SELECT author_principal_id, author_name FROM request_message").fetchall() == [
            (other["id"], other["name"])]


def test_legacy_null_author_reads_as_owner_and_other_principal_cannot_claim(engine, people):
    """Catches NULL author leakage and legacy rows being reassigned on first claim."""
    owner, other, _ = people
    previous_store()
    a = Assistant(engine, ScriptedModel([]))
    assert [x["author"] for x in store.history_page("old")["items"] if x["kind"] == "owner"] == ["owner", "owner"]
    before_store, before_books = sqlite_snapshot(), books_snapshot(engine)
    with pytest.raises(Refusal) as exc:
        a.turn("old", "continue", principal=other)
    assert exc.value.code == "no_conversation"
    assert sqlite_snapshot() == before_store and books_snapshot(engine) == before_books
    assert store.claim("old", "continue", principal_id=owner["id"], principal_name=owner["name"], create=False)


def test_create_records_owner_and_cannot_transfer_it():
    """Catches create overwriting an existing conversation owner."""
    store.create_conversation("mine", "first")
    before = sqlite_snapshot()
    store.create_conversation("mine", "second")
    assert sqlite_snapshot() == before


def test_api_create_records_owner_and_unknown_and_not_yours_have_identical_404_bytes(engine, people):
    """Catches API auto-creation and a different not-yours refusal body."""
    owner, other, token = people
    app = create_app(engine, ScriptedModel([]), embedded_owner=True)
    client = TestClient(app)
    cid = client.post("/v1/conversations").json()["id"]
    with sqlite3.connect(store.path()) as conn:
        assert conn.execute("SELECT owner_principal_id FROM conversation WHERE id = ?", (cid,)).fetchone() == (owner["id"],)
    headers = {"Authorization": f"Bearer {token}"}
    theirs = client.post("/v1/conversations", headers=headers).json()["id"]
    with sqlite3.connect(store.path()) as conn:
        assert conn.execute("SELECT owner_principal_id FROM conversation WHERE id = ?", (theirs,)).fetchone() == (other["id"],)
    before_store, before_books = sqlite_snapshot(), books_snapshot(engine)
    unknown = client.post("/v1/conversations/unknown/messages", json={"text": "hello"}, headers=headers)
    denied = client.post(f"/v1/conversations/{cid}/messages", json={"text": "hello"}, headers=headers)
    assert unknown.status_code == denied.status_code == 404
    assert unknown.headers == denied.headers
    assert unknown.content == denied.content == b'{"detail":"That conversation was not found.","code":"no_conversation"}'
    assert sqlite_snapshot() == before_store
    assert books_snapshot(engine) == before_books
    assert client.post(f"/v1/conversations/{theirs}/messages", json={"text": "hello"}, headers=headers).status_code == 200
