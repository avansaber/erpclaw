"""CLI, live journal, logs, responses and hash-only authenticators retain no typed credential."""
from __future__ import annotations

import json
import logging
import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import bookdiff
from erpclaw_api.app import create_app
from erpclaw_api.pipe import ListenerApp
from erpclaw_assistant.model import ScriptedModel
from erpclaw_cli import journal as journal_mod
from erpclaw_cli.main import _plain, message_for
from erpclaw_core import administration as admin, models as m, principals as pr
from test_no_stored_credentials_chat import REFUSED_CODE, REFUSED_TEXT, absent, probe, route


@pytest.mark.parametrize("hosted_route", (True, False))
def test_cli_surfaces_refusal_and_stores_nothing_new(engine, tmp_path, caplog, hosted_route):
    value = probe("assignment")
    free = message_for(None, "please use " + value)
    assert free["text"] == "please use " + value
    question = {"kind": "field", "choices": [{"label": "First choice", "code": "first"},
                                               {"label": "Second choice", "code": "second"}]}
    assert message_for(question, "2") == {"text": "Second choice", "choice": "second"}
    assert message_for(question, value)["text"] == value
    mdl = route(engine, hosted_route)
    client = TestClient(create_app(engine, mdl, embedded_owner=True))
    cid = client.post("/v1/conversations").json()["id"]
    with caplog.at_level(logging.WARNING):
        events = client.post(f"/v1/conversations/{cid}/messages", json=free).json()["events"]
    assert events[-1]["code"] == REFUSED_CODE
    assert _plain(events[-1]["text"]) == REFUSED_TEXT
    absent(value, events)
    absent(value, caplog.text)
    folder = journal_mod.folder()
    assert not folder.exists() or not list(folder.glob("*.json"))


def test_journal_shape_holds_no_owner_text(engine, tmp_path):
    from erpclaw_core import approvals
    from erpclaw_core.plugins import REGISTRY

    value = probe("private-key-body")
    card = approvals.propose(engine, REGISTRY.contracts["add_customer"], {"name": "Plain Customer"})
    jour = journal_mod.Journal(tmp_path / "approvals", "books-" + secrets.token_hex(4))
    entry = jour.publish(card.approval_id, card.digest, "conversation")
    try:
        assert entry.path.exists() and list(jour.folder.glob("*.json")) == [entry.path]
        data = json.loads(entry.path.read_text())
        assert set(data) == {"approval_id", "digest", "conversation_id", "books", "time"}
        absent(value, data)
        assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"
    finally:
        jour.finish(entry)
    assert not entry.path.exists()


@pytest.mark.parametrize("hosted_route", (True, False))
def test_logs_responses_and_usage_hold_no_value(engine, caplog, hosted_route):
    value = probe("assignment")
    mdl = route(engine, hosted_route)
    client = TestClient(create_app(engine, mdl, embedded_owner=True))
    cid = client.post("/v1/conversations").json()["id"]
    with caplog.at_level(logging.WARNING):
        response = client.post(f"/v1/conversations/{cid}/messages", json={"text": "use " + value})
        assert response.json()["events"][-1]["code"] == REFUSED_CODE
        invalid = client.post(f"/v1/conversations/{cid}/messages", json={"text": {"wrong": value}})
        assert invalid.status_code == 422 and invalid.json()["code"] == "invalid_request"
        missing = client.post("/v1/cards/" + value + "/approve", json={"digest": "d" * 64})
        assert missing.status_code == 404
    for held in (response.text, invalid.text, missing.text, caplog.text, repr(mdl.usage)):
        absent(value, held)
    assert mdl.usage[-1]["refused"] == "password_assignment"


def test_sign_in_and_tokens_stay_hash_only(engine, caplog):
    with engine.begin() as conn:
        who = pr.owner(conn)
        token = admin.issue_token(conn, who["name"])
        code = admin.mint_sign_in_code(conn, who["name"], actor="owner", run_id=None)
    app = create_app(engine, ScriptedModel([]))
    client = TestClient(ListenerApp(app, "host", port=8740), base_url="http://127.0.0.1:8740")
    # This header is intentionally outside owner-prose scanning and remains truncated verbatim.
    agent = "test " + "tok" + "en=" + "x" * 220
    issued = client.post("/v1/session", json={"sign_in_code": code}, headers={"User-Agent": agent})
    assert issued.status_code == 200
    secret = issued.json()["session_secret"]
    assert issued.text.count(secret) == 1
    headers = {"Authorization": "Session " + secret}
    later = [client.post("/v1/session", json={"sign_in_code": code}),
             client.post("/v1/conversations", headers=headers),
             client.delete("/v1/session", headers=headers)]
    assert later[0].status_code == 401 and later[1].status_code == 200 and later[2].status_code == 204
    with engine.connect() as conn:
        assert conn.execute(select(m.principal.c.token_sha256).where(m.principal.c.id == who["id"])).scalar_one() == pr.token_digest(token)
        assert conn.execute(select(m.principal_sign_in.c.code_sha256)).scalar_one() == pr.code_digest(code)
        row = conn.execute(select(m.principal_session)).mappings().one()
        assert row["secret_sha256"] == pr.session_digest(secret)
        assert row["user_agent"] == agent[:200] and row["ended_at"] is not None
    books = json.dumps({t: list(rows.values()) for t, rows in bookdiff.snapshot(engine).items()}, default=str)
    for raw in (token, code, secret):
        for held in (books, caplog.text, [r.text for r in later]):
            absent(raw, held)
    assert token not in issued.text and code not in issued.text
