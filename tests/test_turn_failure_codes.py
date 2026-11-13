"""Model failures end a turn through the same durable refusal path."""
from __future__ import annotations

import re
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from erpclaw_api.app import create_app
from erpclaw_assistant import store
from erpclaw_assistant.model import Reply, ScriptedModel, call, clean_reply
from erpclaw_core import models as m
from erpclaw_core.common import Refusal
from test_model_profiles import OPENROUTER, FakeServer, completion, model
from test_muse import FakeMuse, make as muse_model, resp, text as muse_text


BUSY = "The model service is busy right now."
INTERRUPTED = "The answer was cut short before it finished."


class FailingModel(ScriptedModel):
    def complete(self, messages, tools):
        if self.replies and isinstance(self.replies[0], Refusal):
            self.seen.append(messages)
            raise self.replies.pop(0)
        return super().complete(messages, tools)


def client(engine, mdl):
    c = TestClient(create_app(engine, mdl, embedded_owner=True))
    return c, c.post("/v1/conversations").json()["id"]


def post(c, cid, text="hello"):
    response = c.post(f"/v1/conversations/{cid}/messages", json={"text": text})
    assert response.status_code == 200, response.text
    return response.json()["events"]


def durable_refusal(cid, events, code):
    with sqlite3.connect(store.path()) as conn:
        rid, state, why = conn.execute(
            "SELECT id, state, ended_why FROM request WHERE conversation_id = ?", (cid,)).fetchone()
        assert state == "ended" and why == "refused"
        assert conn.execute("SELECT claimed_by FROM conversation WHERE id = ?", (cid,)).fetchone() == (None,)
        assert conn.execute("SELECT count(*) FROM request_event WHERE request_id = ?", (rid,)).fetchone() == (1,)
    stored = [{k: v for k, v in e.items() if k != "currency"} for e in events]
    assert store.events(rid) == stored
    replay = [event for kind, event, _ in store.timeline(rid) if kind == "engine"]
    assert replay == stored and replay[0]["code"] == code
    assert store.events(rid) == stored  # replaying does not consume or duplicate the event


def plain_sentence(text):
    assert not re.search(r"\d+", text)
    assert not re.search(r"\b[\w]+[_-][\w-]+\b", text)
    assert not re.search(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", text, re.I)
    assert "model_rate_limited" not in text and "answer_interrupted" not in text


@pytest.mark.parametrize("first", [429, 503])
def test_rate_limit_after_one_retry_is_a_durable_refusal(engine, first):
    fake = FakeServer(first, 429, completion("Hello."))
    c, cid = client(engine, model(OPENROUTER, fake))
    events = post(c, cid)
    assert events == [{"type": "refused", "code": "model_rate_limited", "text": BUSY, "currency": "USD"}]
    assert all(e.get("code") != "model_unavailable" for e in events)
    assert len(fake.bodies) == 2
    plain_sentence(events[0]["text"])
    durable_refusal(cid, events, "model_rate_limited")
    assert post(c, cid)[-1]["type"] == "answer"
    assert len(fake.bodies) == 3


@pytest.mark.parametrize("first", [429, 503])
def test_muse_rate_limit_after_one_retry_is_a_durable_refusal(engine, first):
    sleeps = []
    fake = FakeMuse((first, {"retry-after": "1"}), 429, resp([muse_text("Hello.")]))
    mdl = muse_model(fake, sleeps)
    c, cid = client(engine, mdl)
    events = post(c, cid)
    assert events == [{"type": "refused", "code": "model_rate_limited", "text": BUSY, "currency": "USD"}]
    assert all(e.get("code") != "model_unavailable" for e in events)
    assert len(fake.posts()) == 2 and sleeps == [1.0]
    assert mdl.usage[-1]["statuses"] == [first, 429]
    plain_sentence(events[0]["text"])
    durable_refusal(cid, events, "model_rate_limited")
    assert post(c, cid)[-1]["type"] == "answer"
    assert len(fake.posts()) == 3 and sleeps == [1.0]


def test_muse_rate_limit_that_succeeds_on_retry_is_an_answer(engine):
    sleeps = []
    fake = FakeMuse((429, {"retry-after": "1"}), resp([muse_text("Hello.")]))
    mdl = muse_model(fake, sleeps)
    c, cid = client(engine, mdl)
    events = post(c, cid)
    assert [e["type"] for e in events] == ["answer"]
    assert events[0]["text"] == "Hello."
    assert len(fake.posts()) == 2 and sleeps == [1.0]
    assert mdl.usage[-1]["statuses"] == [429, 200]


@pytest.mark.parametrize("status", [500, 503, 504])
@pytest.mark.parametrize("rate_first", [False, True])
def test_muse_server_failures_keep_their_code(engine, status, rate_first):
    first = 429 if rate_first else status
    sleeps = []
    fake = FakeMuse((first, {"retry-after": "1"}), status)
    mdl = muse_model(fake, sleeps)
    c, cid = client(engine, mdl)
    events = post(c, cid)
    assert events == [{"type": "refused", "code": "model_unavailable", "currency": "USD",
                       "text": "The Muse model is unavailable right now. Nothing was saved."}]
    assert len(fake.posts()) == 2 and sleeps == [1.0]
    assert mdl.usage[-1]["statuses"] == [first, status]
    durable_refusal(cid, events, "model_unavailable")


@pytest.mark.parametrize("content,with_call", [("Partial answer", False), ("", False), ("Partial answer", True)])
def test_unfinished_answer_is_a_durable_refusal(engine, content, with_call):
    calls = ([call("create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 2}])]
             if with_call else [])
    reply = clean_reply(content, [(c.id, c.name, c.raw) for c in calls], "length")
    mdl = ScriptedModel([reply, Reply("Hello.")])
    c, cid = client(engine, mdl)
    with engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(m.approval)).scalar_one()
    events = post(c, cid, "invoice Acme Corporation for 2 copper wire" if with_call else "hello")
    assert events == [{"type": "refused", "code": "answer_interrupted", "text": INTERRUPTED, "currency": "USD"}]
    plain_sentence(events[0]["text"])
    durable_refusal(cid, events, "answer_interrupted")
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(m.approval)).scalar_one() == before
    assert post(c, cid)[-1]["type"] == "answer" and len(mdl.seen) == 2


def test_wire_length_finish_is_an_interrupted_answer(engine):
    fake = FakeServer(completion("Partial answer", finish="length"))
    c, cid = client(engine, model(OPENROUTER, fake))
    events = post(c, cid)
    assert events == [{"type": "refused", "code": "answer_interrupted", "text": INTERRUPTED, "currency": "USD"}]
    durable_refusal(cid, events, "answer_interrupted")


@pytest.mark.parametrize("failure,code", [
    ("credential", "credential_refused"),
    (404, "model_refused"),
    (401, "model_refused"),
    (500, "model_unavailable"),
    (502, "model_unavailable"),
    (503, "model_unavailable"),
    (504, "model_unavailable"),
    ("input", "model_input_invalid"),
    ("rate_then_unavailable", "model_unavailable"),
])
def test_other_model_failures_keep_their_codes(engine, failure, code):
    if isinstance(failure, int):
        fake = FakeServer(failure, failure)
        mdl = model(OPENROUTER, fake)
    elif failure == "rate_then_unavailable":
        mdl = model(OPENROUTER, FakeServer(429, 503))
    else:
        mdl = FailingModel([Refusal("The request could not be sent.", code)])
    c, cid = client(engine, mdl)
    events = post(c, cid)
    assert [(e["type"], e.get("code")) for e in events] == [("refused", code)]
    durable_refusal(cid, events, code)


def test_rate_limit_that_succeeds_on_retry_is_an_answer(engine):
    fake = FakeServer(429, completion("Hello."))
    c, cid = client(engine, model(OPENROUTER, fake))
    events = post(c, cid)
    assert len(fake.bodies) == 2
    assert [e["type"] for e in events] == ["answer"]


@pytest.mark.parametrize("finish", ["", "stop", "tool_calls", "function_call", "content_filter"])
def test_other_finish_reasons_keep_their_behavior(engine, finish):
    c, cid = client(engine, ScriptedModel([clean_reply("Hello.", [], finish)]))
    assert [e["type"] for e in post(c, cid)] == ["answer"]
