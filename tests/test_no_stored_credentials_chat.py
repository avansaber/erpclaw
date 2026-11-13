"""A refused owner message retains only labels and the fixed withheld sentence on every route."""
from __future__ import annotations

import json
import secrets
import sqlite3

import pytest
from sqlalchemy import select

from erpclaw_assistant import hosted, store
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import models as m
from erpclaw_core.common import Refusal
from erpclaw_core.db import DEMO_MARK, EVAL_MARK, mark_demo, owner

WITHHELD = "(message withheld: it held a credential)"
REFUSED_TEXT = (
    "That message holds something shaped like a password or key, so it was not "
    "sent to the hosted model. Nothing was saved."
)
REFUSED_CODE = "credential_refused"
GUARD = 12  # the existing shapes' shortest generated assignment value (six hex bytes)
KINDS = ("private-key-body", "assignment", "quoted-assignment", "encoded-json", "nested-json", "multi-match", "unreadable")


def probe(kind):
    name = "pass" + "word"
    if kind == "private-key-body":
        return "-" * 5 + "BEGIN " + "RSA " + "PRIV" + "ATE " + "KE" + "Y" + "-" * 5 + "\n" + secrets.token_hex(20)
    if kind == "assignment":
        return name + " = '" + secrets.token_hex(6) + "'"
    if kind == "quoted-assignment":
        return name + ' = "' + secrets.token_hex(6) + '"'
    if kind == "encoded-json":
        # The name is visible only after JSON decoding.
        return '{"\\u0070' + name[1:] + '": "' + secrets.token_hex(6) + '"}'
    if kind == "nested-json":
        return json.dumps({"memo": json.dumps({"ap" + "i_" + "ke" + "y": secrets.token_hex(6)})})
    if kind == "multi-match":
        return probe("private-key-body") + " and then " + probe("quoted-assignment")
    assert kind == "unreadable"
    return "{" * 5000


def absent(value, held):
    """No long part of a planted value may survive, even after JSON escaping."""
    blob = str(held)
    for text in (value, json.dumps(value)[1:-1]):
        for at in range(max(1, len(text) - GUARD)):
            assert text[at:at + GUARD + 1] not in blob, "a long part of the planted value survived"


class RecordingModel(ScriptedModel):
    def __init__(self, hosted_route=True):
        super().__init__([Reply("Hello.")] * 5)
        self.hosted = hosted_route
        self.hosted_books = "seeded_only"
        self.usage = []
        self.sent = []

    def complete(self, messages, tools):
        if self.hosted:
            hosted.refuse_credentials(self.usage, messages, tools)
        self.sent.append(json.loads(json.dumps(messages)))
        return super().complete(messages, tools)


def route(engine, hosted_route):
    with engine.begin() as conn:
        conn.execute(owner.delete().where(owner.c.mark.in_((DEMO_MARK, EVAL_MARK))))
    if hosted_route:
        mark_demo(engine, EVAL_MARK)
    return RecordingModel(hosted_route)


def stored(cid):
    assert store.path().exists(), "a refused turn must retain a label-only message"
    with sqlite3.connect(store.path()) as conn:
        messages = conn.execute("SELECT text FROM request_message WHERE conversation_id = ?", (cid,)).fetchall()
        dump = "\n".join(conn.iterdump())
        requests = [r[0] for r in conn.execute("SELECT id FROM request WHERE conversation_id = ?", (cid,))]
    return [r[0] for r in messages], dump, requests


def assert_refusal(events):
    assert len(events) == 1
    assert events[0] == {"type": "refused", "code": REFUSED_CODE, "text": REFUSED_TEXT, "currency": "USD"}


def assert_only_labels(text, labels):
    assert WITHHELD in text
    remaining = text.replace(WITHHELD, "")
    for label in labels:
        assert label in remaining
        remaining = remaining.replace(label, "")
    assert not remaining.strip(" \n\t,;:|[]()"), "owner words survived beside the labels"


def check_chat(engine, kind, hosted_route):
    value = probe(kind)
    label = hosted.check(value)
    assert label is not None
    labels = {label, "password_assignment"} if kind == "multi-match" else {label}
    mdl = route(engine, hosted_route)
    assistant = Assistant(engine, mdl)
    cid = "c-" + secrets.token_hex(4)
    with engine.connect() as conn:
        cards = conn.execute(select(m.approval.c.id)).all()
    assert_refusal(assistant.turn(cid, "owner prefix " + value + " owner suffix"))
    assert mdl.sent == []
    texts, dump, requests = stored(cid)
    assert len(texts) == 1
    assert_only_labels(texts[0], labels)
    absent(value, dump)
    absent(value, repr(mdl.usage))
    assert mdl.usage[-1]["refused"] == label
    for rid in requests:
        absent(value, store.timeline(rid))
    absent(value, store.ended_summaries(cid))
    absent(value, store.history_page(cid, after=0))
    with engine.connect() as conn:
        assert conn.execute(select(m.approval.c.id)).all() == cards


@pytest.mark.parametrize("kind", KINDS)
def test_chat_on_hosted_route_keeps_only_label_and_sentence(engine, kind):
    check_chat(engine, kind, True)


@pytest.mark.parametrize("kind", KINDS)
def test_chat_on_local_route_is_scanned_before_storage(engine, kind):
    check_chat(engine, kind, False)


@pytest.mark.parametrize("kind", ("private-key-body", "quoted-assignment", "encoded-json"))
@pytest.mark.parametrize("where", ("user", "arguments", "tool"))
@pytest.mark.parametrize("backend", ("openrouter", "muse"))
def test_hosted_send_gate_refuses_before_bytes(kind, where, backend):
    # Real clients over stubbed HTTP, independently of the chat guard.
    from test_hosted_guards import muse, openrouter
    from test_model_profiles import FakeServer
    import httpx

    value = probe(kind)
    fake, sent = FakeServer(), []
    mdl = openrouter(fake) if backend == "openrouter" else muse(lambda r: sent.append(r) or httpx.Response(500))
    message = {"role": "user", "content": value}
    if where == "arguments":
        message = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": json.dumps({"memo": value})}}]}
    elif where == "tool":
        message = {"role": "tool", "tool_call_id": "c1", "content": value}
    messages = [{"role": "user", "content": "hello"}]
    if where == "tool":
        messages.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]})
    messages.append(message)
    with pytest.raises(Refusal) as exc:
        mdl.complete(messages, [])
    assert exc.value.code == REFUSED_CODE and exc.value.message == REFUSED_TEXT
    assert fake.bodies == [] and sent == []
    assert mdl.usage[-1]["refused"] == hosted.check(value)
    absent(value, repr(mdl.usage))


@pytest.mark.parametrize("hosted_route", (True, False))
def test_refused_turn_leaves_only_label_in_demand_and_history(engine, hosted_route):
    mdl = route(engine, hosted_route)
    assistant = Assistant(engine, mdl)
    value, cid = probe("private-key-body"), "history"
    assert_refusal(assistant.turn(cid, "first use " + value))
    events = assistant.turn(cid, "hello")
    assert events[-1]["type"] == "answer" and len(mdl.sent) == 1
    texts, dump, requests = stored(cid)
    assert texts.count("hello") == 1
    absent(value, dump)
    absent(value, mdl.sent)
    absent(value, store.ended_summaries(cid))
    absent(value, store.history_page(cid, after=0))
    for rid in requests:
        absent(value, store.timeline(rid))


@pytest.mark.parametrize("hosted_route", (False, True), ids=("local", "hosted"))
def test_credential_in_model_tool_arguments_ends_turn_without_storing_reply(engine, as_released, hosted_route):
    from test_model_profiles import FakeServer, LMSTUDIO, OPENROUTER, completion, model, synthetic_key

    value = synthetic_key()
    tool_call = {"id": "c1", "type": "function", "function": {
        "name": "find_capability", "arguments": json.dumps({"request": "stock " + value})}}
    fake = FakeServer(completion("", calls=[tool_call], finish="tool_calls"), completion("Hello."))
    if hosted_route:
        mark_demo(engine, EVAL_MARK)
    mdl = model(OPENROUTER if hosted_route else LMSTUDIO, fake)
    assistant, cid = Assistant(engine, mdl), "model-arguments"
    with engine.connect() as conn:
        cards = conn.execute(select(m.approval.c.id)).all()
    try:
        events = assistant.turn(cid, "what can you do for my stock?")
        assert_refusal(events)
        assert len(fake.bodies) == 1
        assert mdl.usage[-1]["refused"] == "openrouter_key"
        absent(value, mdl.usage)
        texts, dump, requests = stored(cid)
        assert texts == ["what can you do for my stock?"]
        absent(value, dump)
        assert WITHHELD not in dump and "find_capability" not in dump
        assert store.open_request(cid) is None
        with sqlite3.connect(store.path()) as conn:
            assert conn.execute("SELECT claimed_by FROM conversation WHERE id = ?", (cid,)).fetchone() == (None,)
            assert conn.execute("SELECT COUNT(*) FROM capability_demand").fetchone() == (0,)
        with engine.connect() as conn:
            assert conn.execute(select(m.approval.c.id)).all() == cards
        for rid in requests:
            assert store.events(rid) == [{k: v for k, v in events[0].items() if k != "currency"}]
        assert assistant.turn(cid, "hello")[-1]["type"] == "answer"
        assert len(fake.bodies) == 2
        absent(value, fake.bodies)
        absent(value, stored(cid)[1])
        absent(value, store.ended_summaries(cid))
        absent(value, store.history_page(cid, after=0))
    finally:
        mdl.client.close()


CONTROLS = ("sec" + "ret santa", "tok" + "en of thanks", "please send the reset link",
            "the bearer of this invoice", "a short passwordish word",
            "Please invoice for 5 copper wire at 28.00 each, PO 4471, document INV-2026-00007, ap@acme.example.")


@pytest.mark.parametrize("text", CONTROLS)
@pytest.mark.parametrize("hosted_route", (True, False))
def test_ordinary_words_are_stored_and_sent(engine, text, hosted_route):
    assert hosted.check(text) is None
    mdl = route(engine, hosted_route)
    cid = "ordinary"
    events = Assistant(engine, mdl).turn(cid, text)
    assert not [e for e in events if e.get("code") == REFUSED_CODE]
    assert len(mdl.sent) == 1
    assert text in json.dumps(mdl.sent)
    assert text in stored(cid)[0]


@pytest.mark.parametrize("hosted_route", (True, False))
def test_refused_turn_retry_and_pending_question_remain_safe(engine, hosted_route):
    mdl = route(engine, hosted_route)
    assistant = Assistant(engine, mdl)
    cid = "pending"
    mdl.replies = [Reply("", [call("create_invoice", items=[{"item": "copper wire", "quantity": 5}])])]
    assistant.turn(cid, "Invoice for 5 copper wire")
    pending = store.open_request(cid)
    assert pending and pending["pending_question"]
    value = probe("assignment")
    sent_before = list(mdl.sent)
    events = assistant.turn(cid, value, client_message_id="refused")
    assert_refusal(events)
    assert assistant.turn(cid, value, client_message_id="refused") == events
    assert store.open_request(cid) == pending
    assert mdl.sent == sent_before
    texts, dump, _ = stored(cid)
    assert len(texts) == 2
    assert_only_labels(texts[-1], {"password_assignment"})
    absent(value, dump)
