"""The API has one error body, strict inputs and a private health response."""
from __future__ import annotations

import logging

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from bookdiff import assert_changes, snapshot
from conftest import drop_everything
from erpclaw_api import app as api
from erpclaw_api.app import Approval, Message, create_app
from erpclaw_assistant import store
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import administration as admin
from erpclaw_core import approvals, models as m, outbox, setup
from erpclaw_core.common import Refusal, check_events, company_ctx, new_id
from erpclaw_core.plugins import REGISTRY, disable_plugin, install_plugin
from erpclaw_sales import tables as t
from slot_support import replace_filler

ARGS = {"customer": "Acme Corporation", "items": [{"item": "copper wire", "quantity": 2}]}
INVOICE = call("create_invoice", **ARGS)
SAID = "invoice Acme Corporation for 2 copper wire"
DIGEST = "0" * 64
INTERNAL = "The engine hit an internal error."
NO_CONVERSATION = "That conversation was not found."


@pytest.fixture
def email_delivery(engine, monkeypatch):
    """Run delivery through the email connector with an approved test sender."""
    with engine.begin() as conn:
        disable_plugin(conn, "email_folder")
    install_plugin(engine, "email")

    def message_sender(ctx):
        return outbox.SenderSnapshot("books@stark.example", "Stark Books", None, None)

    def send_message(settings, msg, mark_data):
        if not mark_data():
            return outbox.SendOutcome("before_data", "lost_ownership", None, "")
        return outbox.SendOutcome("after_data", "accepted", 250, "2.0.0 queued")

    replace_filler(monkeypatch, "message_sender", message_sender)
    replace_filler(monkeypatch, "send_message", send_message)


def _app(engine, replies=(), *, embedded=True, raise_server_exceptions=True):
    return TestClient(create_app(engine, ScriptedModel(list(replies)), embedded_owner=embedded),
                      raise_server_exceptions=raise_server_exceptions)


def _person(engine, name, can):
    with engine.begin() as conn:
        token = admin.add_principal(conn, name, can, [company_ctx(conn).company_id])
        return {"Authorization": f"Bearer {token}"}


def _card(engine, conversation=None):
    return approvals.propose(engine, REGISTRY.contracts["create_invoice"], ARGS, conversation)


def _status(engine, aid):
    with engine.connect() as conn:
        return conn.execute(select(m.approval.c.status).where(m.approval.c.id == aid)).scalar_one()


def _invoices(engine):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(t.invoice)).scalar_one()


def refused(r, status: int, code: str) -> dict:
    """The one error body: `detail` a non-empty string and `code` the refusal's; nothing else but a 422's `fields`."""
    assert r.status_code == status, (r.status_code, r.text)
    assert r.headers["content-type"].startswith("application/json"), r.headers
    body = r.json()
    assert isinstance(body, dict) and isinstance(body.get("detail"), str) and body["detail"].strip(), body
    assert body.get("code") == code, body
    assert set(body) == ({"detail", "code", "fields"} if status == 422 else {"detail", "code"}), body
    return body


# --- one error body ---------------------------------------------------------------------------------------------

AUTHENTICATED = [("post", "/v1/conversations", None),
                 ("post", "/v1/conversations/{cid}/messages", {"text": "hello"}),
                 ("post", "/v1/cards/{aid}/approve", {"digest": DIGEST}),
                 ("post", "/v1/cards/{aid}/decline", None),
                 ("get", "/v1/operations/{key}", None)]


@pytest.mark.parametrize("auth", [None, " ".join(('Bearer', 'nope')), "Basic abc"])
@pytest.mark.parametrize("method, path, body", AUTHENTICATED, ids=[f"{a} {p}" for a, p, _ in AUTHENTICATED])
def test_every_authenticated_route_refuses_a_missing_or_bad_credential_as_401_unauthenticated(engine, auth, method,
                                                                                               path, body):
    c = _app(engine, embedded=False)
    url = path.format(cid=new_id(), aid=new_id(), key=new_id())
    r = getattr(c, method)(url, headers={"Authorization": auth} if auth else {},
                           **({"json": body} if body is not None else {}))
    refused(r, 401, "unauthenticated")


def test_a_message_from_a_principal_without_view_is_403_forbidden(engine):
    c = _app(engine, embedded=False)
    who = _person(engine, "erin", ["export"])
    cid = c.post("/v1/conversations", headers=who).json()["id"]
    refused(c.post(f"/v1/conversations/{cid}/messages", json={"text": "hello"}, headers=who), 403, "forbidden")


@pytest.mark.parametrize("action", ["approve", "decline"])
def test_a_card_action_without_the_permission_is_403_forbidden(engine, action):
    c = _app(engine, embedded=False)
    card = _card(engine)
    who = _person(engine, "vic", ["view"] if action == "decline" else ["view", "draft"])
    kw = {"json": {"digest": card.digest}} if action == "approve" else {}
    refused(c.post(f"/v1/cards/{card.approval_id}/{action}", headers=who, **kw), 403, "forbidden")
    assert _status(engine, card.approval_id) == "pending"


def test_an_operation_read_without_view_is_403_forbidden(engine):
    card = _card(engine)
    c = _app(engine, embedded=False)
    who = _person(engine, "olga", ["export"])
    refused(c.get(f"/v1/operations/{card.operation_key}", headers=who), 403, "forbidden")


@pytest.mark.parametrize("action", ["approve", "decline"])
def test_a_card_the_books_do_not_have_is_404_no_card(engine, action):
    c = _app(engine)
    kw = {"json": {"digest": DIGEST}} if action == "approve" else {}
    refused(c.post(f"/v1/cards/{new_id()}/{action}", **kw), 404, "no_card")


def test_an_approval_whose_digest_differs_is_409_with_the_refusals_code(engine):
    card = _card(engine)
    c = _app(engine)
    refused(c.post(f"/v1/cards/{card.approval_id}/approve", json={"digest": "f" * 64}), 409, "digest")
    assert _status(engine, card.approval_id) == "pending"


@pytest.mark.parametrize("method, path", [("get", "/v1/nothing"), ("post", "/v2/conversations"), ("get", "/")])
def test_a_route_that_does_not_exist_is_404_not_found(engine, method, path):
    refused(getattr(_app(engine), method)(path), 404, "not_found")


@pytest.mark.parametrize("method, path", [("get", "/v1/health/"), ("post", "/v1/conversations/")])
def test_a_trailing_slash_is_404_not_found_never_a_redirect(engine, method, path):
    r = getattr(_app(engine), method)(path, follow_redirects=False)
    refused(r, 404, "not_found")
    assert "location" not in r.headers


@pytest.mark.parametrize("method, path", [("get", "/v1/conversations"), ("delete", "/v1/health"),
                                          ("get", "/v1/cards/x/approve"), ("put", "/v1/operations/x")])
def test_a_known_route_with_the_wrong_method_is_405_method_not_allowed(engine, method, path):
    refused(getattr(_app(engine), method)(path), 405, "method_not_allowed")


MARK = "Zq7SecretValue"
INVALID = [
    ("message, empty text", "messages", {"text": ""}, [["body", "text"]]),
    ("message, text too long", "messages", {"text": MARK + "x" * 4000}, [["body", "text"]]),
    ("message, text not a string", "messages", {"text": 987654321}, [["body", "text"]]),
    ("message, choice too long", "messages", {"text": "hi", "choice": MARK * 30}, [["body", "choice"]]),
    ("message, no body", "messages", None, [["body"]]),
    ("approval, short digest", "approve", {"digest": MARK}, [["body", "digest"]]),
    ("approval, no digest", "approve", {}, [["body", "digest"]]),
]


@pytest.mark.parametrize("case, route, body, fields", INVALID, ids=[x[0] for x in INVALID])
def test_a_body_failing_validation_is_422_invalid_request_naming_the_field_never_its_value(engine, case, route, body,
                                                                                         fields):
    c = _app(engine)
    url = f"/v1/conversations/{new_id()}/messages" if route == "messages" else f"/v1/cards/{new_id()}/approve"
    got = refused(c.post(url, **({"json": body} if body is not None else {})), 422, "invalid_request")
    assert got["fields"] == fields, got
    assert MARK not in str(got) and "987654321" not in str(got), got  # never the value sent


def test_a_body_that_is_not_json_is_422_invalid_request(engine):
    r = _app(engine).post(f"/v1/conversations/{new_id()}/messages", content=b'{"text": ',
                          headers={"content-type": "application/json"})
    got = refused(r, 422, "invalid_request")
    assert got["fields"] and all(isinstance(p, list) and p[0] == "body" for p in got["fields"]), got


def test_an_unexpected_exception_is_500_internal_error_with_fixed_text_and_its_transaction_rolls_back(engine,
                                                                                                    monkeypatch):
    card = _card(engine)
    contract = REGISTRY.contracts["create_invoice"]
    real = contract.commit

    def commit(ctx, inp, key):
        real(ctx, inp, key)  # the invoice is written, then the effect fails in a way nothing expected
        raise RuntimeError(f"{MARK} at the database layer")

    monkeypatch.setattr(contract, "commit", commit)
    before, n = snapshot(engine), _invoices(engine)
    r = _app(engine, raise_server_exceptions=False).post(f"/v1/cards/{card.approval_id}/approve",
                                                         json={"digest": card.digest})
    assert refused(r, 500, "internal_error")["detail"] == INTERNAL
    assert MARK not in r.text and "RuntimeError" not in r.text
    assert _invoices(engine) == n and _status(engine, card.approval_id) == "pending"
    assert_changes(before, snapshot(engine), {})


def _result_event(outcome):
    return {"type": "result", "status": outcome.status, "text": outcome.text,
            "operation_key": outcome.operation_key, "amounts": outcome.amounts, "currency": "USD"}


def _followup_warning(caplog):
    records = [r for r in caplog.records if r.name == api.__name__]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "RuntimeError" in records[0].getMessage()
    assert MARK not in records[0].getMessage() and records[0].exc_info is None


@pytest.mark.parametrize("step", [None, "delivery", "history"])
def test_initial_setup_responses_carry_the_committed_currency(engine, monkeypatch, caplog, step):
    drop_everything(engine)
    setup.create_own_books(engine)
    card = approvals.propose(engine, REGISTRY.contracts["set_up_company"], {
        "name": "Lakeside Bakery", "currency": "USD", "fiscal_year_start_month": 1, "date_order": "DMY"},
        new_id())
    assert approvals.books_currency(engine, approval_id=card.approval_id) is None
    c = _app(engine, raise_server_exceptions=False)
    notes = []
    real_note = c.app.state.assistant.note

    def note(*args, **kwargs):
        notes.append(args)
        if step == "history":
            raise RuntimeError(MARK)
        return real_note(*args, **kwargs)

    def fail_delivery(eng):
        raise RuntimeError(MARK)

    monkeypatch.setattr(c.app.state.assistant, "note", note)
    if step == "delivery":
        monkeypatch.setattr(api, "deliver_pending", fail_delivery)
    with caplog.at_level(logging.WARNING, logger=api.__name__):
        r = c.post(f"/v1/cards/{card.approval_id}/approve", json={"digest": card.digest})
    assert r.status_code == 200, r.text
    outcome = approvals.read_back(engine, card.operation_key)
    assert outcome.status == "committed" and _status(engine, card.approval_id) == "consumed"
    assert approvals.books_currency(engine, approval_id=card.approval_id) == "USD"
    expected = [_result_event(outcome)]
    if step:
        text = ("It's saved. Sending the email didn't finish." if step == "delivery" else
                "It's saved. Adding it to this conversation's history didn't finish; nothing else is affected.")
        expected.append({"type": "answer", "text": text, "currency": "USD"})
        _followup_warning(caplog)
    assert r.json()["events"] == expected
    assert check_events(expected) == expected
    assert len(notes) == (step != "delivery")
    assert MARK not in r.text


def test_an_invoice_approval_carries_the_books_currency(engine):
    card = _card(engine)
    r = _app(engine).post(f"/v1/cards/{card.approval_id}/approve", json={"digest": card.digest})
    assert r.status_code == 200, r.text
    outcome = approvals.read_back(engine, card.operation_key)
    assert outcome.status == "committed"
    assert r.json()["events"] == [_result_event(outcome)]


@pytest.mark.parametrize("before, carried, expected", [("USD", "EUR", "USD"), (None, "USD", "USD"),
                                                      (None, None, None)])
def test_a_currency_read_failure_after_commit_uses_available_currency(engine, monkeypatch, caplog,
                                                                    before, carried, expected):
    card = _card(engine)
    c = _app(engine, raise_server_exceptions=False)
    contract = REGISTRY.contracts["create_invoice"]
    real_commit, real_currency = contract.commit, approvals.books_currency
    reads = []
    n = _invoices(engine)

    def commit(ctx, inp, key):
        result = real_commit(ctx, inp, key)
        if carried:
            result.data["currency"] = carried
        return result

    def currency(eng, **kwargs):
        reads.append(kwargs)
        if len(reads) == 1:
            assert real_currency(eng, **kwargs) == "USD"
            assert approvals.read_back(eng, card.operation_key).status == "unknown"
            return before
        assert approvals.read_back(eng, card.operation_key).status == "committed"
        raise RuntimeError(MARK)

    monkeypatch.setattr(contract, "commit", commit)
    monkeypatch.setattr(approvals, "books_currency", currency)
    with caplog.at_level(logging.WARNING, logger=api.__name__):
        r = c.post(f"/v1/cards/{card.approval_id}/approve", json={"digest": card.digest})
    assert r.status_code == 200, r.text
    outcome = approvals.read_back(engine, card.operation_key)
    assert outcome.status == "committed" and _invoices(engine) == n + 1
    assert _status(engine, card.approval_id) == "consumed"
    assert len(reads) == 2
    assert r.json()["events"] == [{**_result_event(outcome), "currency": expected}]
    assert MARK not in r.text and "RuntimeError" not in r.text
    _followup_warning(caplog)


def test_a_refused_outcome_with_a_history_failure_uses_the_neutral_sentence(engine, monkeypatch, caplog):
    card = approvals.propose(engine, REGISTRY.contracts["create_invoice"], ARGS, new_id())
    c = _app(engine, raise_server_exceptions=False)
    n = _invoices(engine)
    reads, deliveries = [], []
    real_currency = approvals.books_currency

    def refuse(ctx, inp, key):
        raise Refusal("The write was refused.", "rule_at_commit")

    def fail_history(*args, **kwargs):
        raise RuntimeError(MARK)

    def currency(eng, **kwargs):
        reads.append(kwargs)
        return real_currency(eng, **kwargs)

    monkeypatch.setattr(REGISTRY.contracts["create_invoice"], "commit", refuse)
    monkeypatch.setattr(c.app.state.assistant, "note", fail_history)
    monkeypatch.setattr(approvals, "books_currency", currency)
    monkeypatch.setattr(api, "deliver_pending", lambda eng: deliveries.append(eng) or [])
    with caplog.at_level(logging.WARNING, logger=api.__name__):
        r = c.post(f"/v1/cards/{card.approval_id}/approve", json={"digest": card.digest})
    assert r.status_code == 200, r.text
    outcome = approvals.read_back(engine, card.operation_key)
    assert outcome.status == "refused" and _invoices(engine) == n
    events = r.json()["events"]
    assert events == [{**_result_event(outcome), "code": "rule_at_commit"},
                      {"type": "answer", "currency": "USD", "text":
                       "Adding it to this conversation's history didn't finish; nothing else is affected."}]
    assert "saved" not in events[-1]["text"].lower()
    assert check_events(events) == events and len(reads) == 1 and not deliveries
    _followup_warning(caplog)


@pytest.mark.parametrize("state", ["queued", "sending", "accepted", "unknown", "failed", None])
def test_a_delivery_failure_returns_the_committed_result_and_the_actual_queue_state(engine, monkeypatch, caplog, state,
                                                                                  email_delivery):
    card = approvals.propose(engine, REGISTRY.contracts["create_invoice"], {**ARGS, "email": state is not None})
    c = _app(engine, raise_server_exceptions=False)
    n = _invoices(engine)

    def fail_delivery(eng):
        if state not in (None, "queued"):
            with eng.begin() as conn:
                conn.execute(m.outbox.update().where(m.outbox.c.operation_key == card.operation_key)
                             .values(status=state))
        raise RuntimeError(MARK)

    monkeypatch.setattr(api, "deliver_pending", fail_delivery)
    with caplog.at_level(logging.WARNING, logger=api.__name__):
        r = c.post(f"/v1/cards/{card.approval_id}/approve", json={"digest": card.digest})
    assert r.status_code == 200, r.text
    saved = approvals.read_back(engine, card.operation_key)
    text = ("It's saved. Sending the email didn't finish; the message is still waiting to be sent."
            if state == "queued" else "It's saved. Sending the email didn't finish.")
    events = r.json()["events"]
    assert events == [_result_event(saved), {"type": "answer", "text": text, "currency": "USD"}]
    assert check_events(events) == events
    assert saved.status == "committed" and _status(engine, card.approval_id) == "consumed"
    assert _invoices(engine) == n + 1
    with engine.connect() as conn:
        rows = conn.execute(select(m.outbox.c.id, m.outbox.c.status)
                            .where(m.outbox.c.operation_key == card.operation_key)).all()
        assert conn.execute(select(func.count()).select_from(t.invoice)
                            .where(t.invoice.c.operation_key == card.operation_key)).scalar_one() == 1
    assert [row.status for row in rows] == ([] if state is None else [state])
    assert any(row.status == "queued" for row in rows) == (state == "queued")
    assert MARK not in r.text and "error" not in r.text.lower()
    _followup_warning(caplog)
    if state == "queued":
        monkeypatch.setattr(api, "deliver_pending", outbox.deliver_pending)
        again = c.post(f"/v1/cards/{card.approval_id}/approve", json={"digest": card.digest})
        assert again.status_code == 200, again.text
        result, email = again.json()["events"]
        assert result == _result_event(saved) and email["type"] == "email" and email["status"] == "accepted"
        with engine.connect() as conn:
            assert conn.execute(select(m.outbox.c.status).where(m.outbox.c.id == rows[0].id)).scalar_one() == "accepted"
        assert _invoices(engine) == n + 1


@pytest.mark.parametrize("action", ["approve", "decline"])
@pytest.mark.parametrize("step", ["note", "conversation_lookup"])
def test_a_history_failure_keeps_the_saved_card_action_and_all_its_events(engine, monkeypatch, caplog, action, step,
                                                                       email_delivery):
    c = _app(engine, [Reply("", [INVOICE])], raise_server_exceptions=False)
    cid = c.post("/v1/conversations").json()["id"]
    c.post(f"/v1/conversations/{cid}/messages", json={"text": SAID})
    req = store.open_request(cid)
    before_history = store.events(req["id"])
    card = approvals.propose(engine, REGISTRY.contracts["create_invoice"], {**ARGS, "email": True}, cid)
    n = _invoices(engine)
    delivered = []

    def delivery(eng):
        results = outbox.deliver_pending(eng)
        delivered.extend(results)
        return results

    def fail_history(*args, **kwargs):
        raise RuntimeError(MARK)

    monkeypatch.setattr(api, "deliver_pending", delivery)
    if step == "note":
        monkeypatch.setattr(c.app.state.assistant, "note", fail_history)
    else:
        monkeypatch.setattr(approvals, "conversation_of", fail_history)
    with caplog.at_level(logging.WARNING, logger=api.__name__):
        r = c.post(f"/v1/cards/{card.approval_id}/{action}",
                   **({"json": {"digest": card.digest}} if action == "approve" else {}))
    assert r.status_code == 200, r.text
    saved = (approvals.read_back(engine, card.operation_key) if action == "approve" else
             approvals.Outcome("declined", "Not saved. Nothing changed in your books.", card.approval_id))
    expected = [_result_event(saved)]
    expected.extend({"type": "email", "status": d["status"], "text": f"Email to {d['recipient']}: {d['text']}"}
                    for d in delivered if d["operation_key"] == card.operation_key)
    expected.append({"type": "answer", "currency": "USD",
                     "text": ("It's saved. " if action == "approve" else "") +
                     "Adding it to this conversation's history didn't finish; nothing else is affected."})
    events = r.json()["events"]
    assert events == expected and check_events(events) == events
    assert _status(engine, card.approval_id) == ("consumed" if action == "approve" else "declined")
    assert _invoices(engine) == n + (action == "approve")
    assert store.events(req["id"]) == before_history
    assert len(delivered) == (action == "approve")
    assert all(d["status"] == "accepted" for d in delivered)
    assert MARK not in r.text and "error" not in r.text.lower()
    _followup_warning(caplog)


def test_a_turns_own_refusal_is_still_an_event_inside_a_200(engine):
    """The error body is for the HTTP layer; a turn's own refusals stay `refused` events in a 200."""
    c = _app(engine, [Reply("", [call("create_invoice", customer="Nobody At All",
                                      items=[{"item": "copper wire", "quantity": 1}])])])
    cid = c.post("/v1/conversations").json()["id"]
    r = c.post(f"/v1/conversations/{cid}/messages", json={"text": "invoice Nobody At All for 1 copper wire"})
    assert r.status_code == 200 and r.json()["events"][-1]["type"] in ("ask", "refused"), r.text


# --- what "additive within v1" covers -----------------------------------------------------------------------------

FROZEN_ROUTES = {("GET", "/v1/health"), ("POST", "/v1/conversations"), ("POST", "/v1/conversations/{cid}/messages"),
                 ("POST", "/v1/cards/{aid}/approve"), ("POST", "/v1/cards/{aid}/decline"),
                 ("GET", "/v1/operations/{key}")}
FROZEN_BODIES = {Message: ({"text"}, {"text", "choice"}), Approval: ({"digest"}, {"digest"})}


def test_v1_routes_and_request_fields_are_only_ever_added_to(engine):
    """No frozen route goes; no request field an older client sends goes; nothing it does not send is required."""
    app = _app(engine).app
    routes = [r for r in app.routes if isinstance(r, APIRoute)]
    assert FROZEN_ROUTES <= {(meth, r.path) for r in routes for meth in r.methods}
    for r in routes:
        assert not [q.name for q in r.dependant.query_params if q.field_info.is_required()], r.path
    for model, (required, declared) in FROZEN_BODIES.items():
        fields = model.model_fields
        assert declared <= set(fields), model.__name__
        assert {n for n, f in fields.items() if f.is_required()} == required, model.__name__


# --- unknown request fields are refused -------------------------------------------------------------------------

@pytest.mark.parametrize("route", ["conversations", "decline"])
@pytest.mark.parametrize("body", [None, {}], ids=["no_body", "empty_object"])
def test_routes_without_fields_accept_an_absent_body_or_an_empty_object(engine, route, body):
    c = _app(engine)
    card = _card(engine)
    url = "/v1/conversations" if route == "conversations" else f"/v1/cards/{card.approval_id}/decline"
    r = c.post(url, **({"json": body} if body is not None else {}))
    assert r.status_code == 200, r.text
    if route == "conversations":
        assert set(r.json()) == {"id"} and r.json()["id"]
    else:
        assert r.json()["events"][0]["status"] == "declined"
    assert _status(engine, card.approval_id) == ("pending" if route == "conversations" else "declined")


@pytest.mark.parametrize("route", ["conversations", "decline"])
def test_routes_without_fields_refuse_extra_fields_before_the_action_runs(engine, monkeypatch, route):
    c = _app(engine)
    card = _card(engine)
    minted = []

    def mint():
        minted.append(new_id())
        return minted[-1]

    monkeypatch.setattr(api, "new_id", mint)
    before = snapshot(engine)
    url = "/v1/conversations" if route == "conversations" else f"/v1/cards/{card.approval_id}/decline"
    got = refused(c.post(url, json={"x": 1}), 422, "invalid_request")
    assert got["fields"] == [["body", "x"]]
    assert not minted and _status(engine, card.approval_id) == "pending"
    assert_changes(before, snapshot(engine), {})


@pytest.mark.parametrize("route, body, field", [
    ("messages", {"text": "hello", "colour": "Zq7red"}, "colour"),
    ("approve", {"digest": DIGEST, "note": "Zq7note"}, "note"),
], ids=["message", "approval"])
def test_a_body_naming_an_undeclared_field_is_422_invalid_request_and_nothing_runs(engine, route, body, field):
    card = _card(engine)
    if route == "approve":
        body = {**body, "digest": card.digest}  # the right digest: only the extra field is wrong
    model = ScriptedModel([Reply("", [INVOICE])])
    c = TestClient(create_app(engine, model, embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    before = snapshot(engine)
    url = f"/v1/conversations/{cid}/messages" if route == "messages" else f"/v1/cards/{card.approval_id}/approve"
    got = refused(c.post(url, json=body), 422, "invalid_request")
    assert got["fields"] == [["body", field]], got
    assert body[field] not in str(got), got
    assert model.seen == [] and store.open_request(cid) is None  # no turn ran, nothing stored
    assert _status(engine, card.approval_id) == "pending"
    assert_changes(before, snapshot(engine), {})


def test_client_message_id_is_accepted_and_a_repeat_replays_with_one_model_call(engine):
    model = ScriptedModel([Reply("Hello.")])
    c = TestClient(create_app(engine, model, embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    first = c.post(f"/v1/conversations/{cid}/messages", json={"text": "hello", "client_message_id": "a1b2c3d4"})
    assert first.status_code == 200
    assert [(event["type"], event["text"]) for event in first.json()["events"]] == [("answer", "Hello.")]
    repeat = c.post(f"/v1/conversations/{cid}/messages", json={"text": "hello", "client_message_id": "a1b2c3d4"})
    assert repeat.status_code == 200 and repeat.content == first.content
    assert len(model.seen) == 1


# --- a conversation the caller may not use ----------------------------------------------------------------------

def test_a_message_to_a_conversation_the_books_do_not_have_is_404_no_conversation(engine):
    model = ScriptedModel([Reply("Hello.", [])])
    c = TestClient(create_app(engine, model, embedded_owner=True))
    got = refused(c.post(f"/v1/conversations/{new_id()}/messages", json={"text": "hello"}), 404, "no_conversation")
    assert got["detail"] == NO_CONVERSATION and model.seen == []


def test_another_principals_conversation_and_an_unknown_one_get_byte_identical_404s(engine):
    c = _app(engine, [Reply("Hello.", [])] * 3, embedded=False)
    dana = _person(engine, "dana", ["view", "draft"])
    ann = _person(engine, "ann", ["view", "draft"])
    own = c.post("/v1/conversations", headers=dana).json()["id"]
    assert c.post(f"/v1/conversations/{own}/messages", json={"text": "hello"}, headers=dana).status_code == 200
    theirs = c.post(f"/v1/conversations/{own}/messages", json={"text": "hello"}, headers=ann)
    unknown = c.post(f"/v1/conversations/{new_id()}/messages", json={"text": "hello"}, headers=ann)
    refused(theirs, 404, "no_conversation")
    assert theirs.content == unknown.content and theirs.status_code == unknown.status_code


def test_health_answers_any_caller_and_names_no_model(engine):
    model = ScriptedModel([])
    model.name = "qwen-secret-deployment-7b"
    c = TestClient(create_app(engine, model))
    r = c.get("/v1/health")
    assert r.status_code == 200 and r.json() == {"ok": True}, r.text
    assert "model" not in r.json() and model.name not in r.text


@pytest.mark.parametrize("action", ["approve", "decline"])
@pytest.mark.xfail(strict=True, reason="needs conversation ownership")
def test_a_card_in_another_principals_conversation_is_forbidden(engine, action):
    c = _app(engine, [Reply("", [INVOICE])], embedded=False)
    dana = _person(engine, "dana", ["view", "draft", "approve", "post"])
    ann = _person(engine, "ann", ["view", "draft", "approve", "post"])
    cid = c.post("/v1/conversations", headers=dana).json()["id"]
    [card] = c.post(f"/v1/conversations/{cid}/messages", json={"text": SAID}, headers=dana).json()["events"]
    before = snapshot(engine)
    kw = {"json": {"digest": card["digest"]}} if action == "approve" else {}
    refused(c.post(f"/v1/cards/{card['approval_id']}/{action}", headers=ann, **kw), 403, "forbidden")
    assert snapshot(engine) == before


def test_decline_notes_the_cards_conversation_even_when_another_is_named(engine):
    c = _app(engine, [Reply("", [INVOICE])] * 2)
    first, second = [c.post("/v1/conversations").json()["id"] for _ in range(2)]
    [card] = c.post(f"/v1/conversations/{first}/messages", json={"text": SAID}).json()["events"]
    c.post(f"/v1/conversations/{second}/messages", json={"text": SAID})
    a, b = store.open_request(first), store.open_request(second)
    assert a and b
    before_a, before_b = store.events(a["id"]), store.events(b["id"])
    r = c.post(f"/v1/cards/{card['approval_id']}/decline", params={"conversation_id": second})
    assert r.status_code == 200, r.text
    text = " ".join(e["text"] for e in r.json()["events"] if e.get("text"))
    assert store.events(a["id"]) == [*before_a, {"type": "note", "text": text}]
    assert store.events(b["id"]) == before_b


def test_decline_uses_the_supplied_conversation_for_a_card_without_one(engine):
    c = _app(engine, [Reply("", [INVOICE])])
    cid = c.post("/v1/conversations").json()["id"]
    c.post(f"/v1/conversations/{cid}/messages", json={"text": SAID})
    req = store.open_request(cid)
    assert req
    before = store.events(req["id"])
    card = _card(engine)
    r = c.post(f"/v1/cards/{card.approval_id}/decline", params={"conversation_id": cid})
    assert r.status_code == 200, r.text
    text = " ".join(e["text"] for e in r.json()["events"] if e.get("text"))
    assert store.events(req["id"]) == [*before, {"type": "note", "text": text}]


def test_validation_lists_each_failing_field_without_its_value(engine):
    r = _app(engine).post(f"/v1/conversations/{new_id()}/messages",
                          json={"text": 987654321, "choice": "", "extra": MARK})
    got = refused(r, 422, "invalid_request")
    assert got["fields"] == [["body", "text"], ["body", "choice"], ["body", "extra"]]
    assert MARK not in r.text and "987654321" not in r.text


def test_query_validation_uses_the_same_error_body(engine):
    c = _app(engine)

    @c.app.get("/query-check")
    def query_check(count: int):
        return {"count": count}

    got = refused(c.get("/query-check", params={"count": MARK}), 422, "invalid_request")
    assert got["fields"] == [["query", "count"]]
    assert MARK not in str(got)


@pytest.mark.parametrize("code", ["fenced", "replaces_mismatch"])
@pytest.mark.parametrize("action", ["approve", "decline"])
def test_card_action_conflicts_keep_the_refusals_code(engine, monkeypatch, action, code):
    from erpclaw_core.common import Refusal

    def refuse(*args, **kwargs):
        raise Refusal("That request has ended; ask again.", code)

    monkeypatch.setattr(approvals, action, refuse)
    kw = {"json": {"digest": DIGEST}} if action == "approve" else {}
    got = refused(_app(engine).post(f"/v1/cards/{new_id()}/{action}", **kw), 409, code)
    assert got["detail"] == "That request has ended; ask again."
