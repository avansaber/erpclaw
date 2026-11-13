"""Owner text at every live HTTP boundary, including concealed failures."""
from datetime import timedelta

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from psycopg.errors import LockNotAvailable
from sqlalchemy import update
from sqlalchemy.exc import OperationalError

from erpclaw_api.app import create_app
from erpclaw_assistant.model import Reply, ScriptedModel
from erpclaw_core import administration as admin, approvals, models as m
from erpclaw_core.common import company_ctx, now
from erpclaw_core.plugins import REGISTRY
from owner_text import event_violations, owner_text_violations
from owner_text import observe_refusal_codes, refusal_code_report

MARKER = "".join(('erpclaw-planted-secret-', '0a1b2c3d'))
ARGS = {"customer": "Acme Corporation", "items": [{"item": "copper wire", "quantity": 5}]}
# A route without a body, authentication, a database read or an identifier cannot
# reach the corresponding failure. These expectations describe those boundaries.
ROUTES = {
    ("GET", "/v1/health"): (False, False, False),
    ("GET", "/v1/me"): (True, False, True),
    ("POST", "/v1/session"): (False, True, True),
    ("DELETE", "/v1/session"): (False, False, True),
    ("POST", "/v1/conversations"): (True, True, True),
    ("POST", "/v1/conversations/{cid}/messages"): (True, True, True),
    ("POST", "/v1/cards/{aid}/approve"): (True, True, True),
    ("POST", "/v1/cards/{aid}/decline"): (True, True, True),
    ("GET", "/v1/operations/{key}"): (True, False, True),
}


class Host:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        scope["erpclaw_listener"] = "host"
        await self.app(scope, receive, send)


def checked(response, status=None):
    assert MARKER not in response.text, response.text
    if status is not None:
        assert response.status_code == status, response.text
    if not response.content:
        return
    body = response.json()
    if "detail" in body:
        bad = owner_text_violations(body["detail"], code=body.get("code"), planted=[MARKER])
        assert not bad, (body["detail"], bad)
    bad = event_violations(body.get("events", []))
    if isinstance(body.get("text"), str):
        bad += owner_text_violations(body["text"])
    assert not bad, bad


@pytest.fixture
def window(engine):
    app = create_app(engine, ScriptedModel([Reply("Hello.")] * 8), embedded_owner=True)
    client = TestClient(app, raise_server_exceptions=False)
    cid = client.post("/v1/conversations").json()["id"]
    card = approvals.propose(engine, REGISTRY.contracts["create_invoice"], ARGS)
    return app, client, cid, card


def request(window, route, **kwargs):
    app, client, cid, card = window
    method, path = route
    path = path.replace("{cid}", cid).replace("{aid}", card.approval_id).replace("{key}", card.operation_key)
    body = ({"text": "hello"} if "messages" in path else {"digest": card.digest} if "approve" in path
            else {"sign_in_code": "z" * 26} if route == ("POST", "/v1/session") else {})
    if method == "POST" and "json" not in kwargs and "content" not in kwargs:
        kwargs["json"] = body
    if path == "/v1/session":
        client = TestClient(Host(app), raise_server_exceptions=False)
    return client.request(method, path, **kwargs)


def test_the_live_route_table_is_exactly_the_sweeps_expectations(window):
    actual = {(method, r.path) for r in window[0].routes if isinstance(r, APIRoute) for method in r.methods}
    assert actual == set(ROUTES), {"missing_cases": actual - set(ROUTES), "removed_routes": set(ROUTES) - actual}


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("authorization", [None, " ".join(('Bearer', 'wrong')), "Session wrong"])
def test_each_routes_authentication_boundary_reads_plainly(window, route, authorization):
    app, _, _, _ = window
    # Disable only the in-process owner convenience using the original dependency's
    # real credential parser and principal lookup through a separately built app.
    engine = app.state.engine
    guarded = create_app(engine, ScriptedModel([]))
    other = (guarded, TestClient(guarded, raise_server_exceptions=False), window[2], window[3])
    headers = {} if authorization is None else {"Authorization": authorization}
    protected = ROUTES[route][0]
    status = 401 if protected else (401 if route == ("POST", "/v1/session") and authorization != " ".join(('Bearer', 'wrong'))
                                  else 400 if route == ("POST", "/v1/session") else 204 if route[0] == "DELETE" else 200)
    checked(request(other, route, headers=headers), status)


BODY_ROUTES = [r for r, (_, body, _) in ROUTES.items() if body]


@pytest.mark.parametrize("route", BODY_ROUTES)
@pytest.mark.parametrize("failure", ["malformed", "unknown", "wrong_type", "oversized"])
def test_body_failures_read_plainly(window, route, failure):
    if failure == "malformed":
        kwargs = {"content": '{"broken":' + MARKER, "headers": {"Content-Type": "application/json"}}
    elif failure == "wrong_type":
        kwargs = {"json": [MARKER]}
    elif failure == "unknown":
        kwargs = {"json": {"unexpected": MARKER}}
    else:
        field = "text" if "messages" in route[1] else "digest" if "approve" in route[1] else "sign_in_code"
        kwargs = {"json": {field: MARKER * 500}}
    checked(request(window, route, **kwargs), 422)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("failure", ["database", "lock", "handler"])
def test_injected_failures_never_reach_the_owner(window, monkeypatch, route, failure):
    app = window[0]
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        if failure == "database":
            raise OperationalError("SELECT planted", {}, RuntimeError(MARKER))
        if failure == "lock":
            raise LockNotAvailable(MARKER)
        raise RuntimeError(MARKER)

    with monkeypatch.context() as injected:
        if failure == "handler":
            endpoint = next(r for r in app.routes if isinstance(r, APIRoute) and r.path == route[1] and route[0] in r.methods)
            injected.setattr(endpoint.dependant, "call", fail)
        elif ROUTES[route][2]:
            injected.setattr(app.state.engine, "connect", fail)
        response = request(window, route, headers={"Authorization": "Session wrong"} if route[0] == "DELETE" else {})
        checked(response, 500 if failure == "handler" or ROUTES[route][2] else 200)
        assert bool(calls) == (failure == "handler" or ROUTES[route][2])


@pytest.mark.parametrize("target", ["message", "approve", "decline", "operation"])
def test_unknown_identifiers_read_plainly(window, target):
    _, client, _, card = window
    path = {"message": "/v1/conversations/absent/messages", "approve": "/v1/cards/absent/approve",
            "decline": "/v1/cards/absent/decline", "operation": "/v1/operations/absent"}[target]
    response = client.get(path) if target == "operation" else client.post(
        path, json={"text": "hello"} if target == "message" else {"digest": card.digest} if target == "approve" else {})
    checked(response, 200 if target == "operation" else 404)


@pytest.mark.parametrize("route", [r for r in ROUTES if r[0] == "POST" and ("messages" in r[1] or "cards" in r[1])]
                         + [("GET", "/v1/operations/{key}")])
def test_missing_permissions_read_plainly(window, route):
    app, _, cid, card = window
    with app.state.engine.begin() as conn:
        token = admin.add_principal(conn, "reader", [], [company_ctx(conn).company_id])
    response = request(window, route, headers={"Authorization": "Bearer " + token})
    checked(response)
    assert response.status_code in (403, 404), response.text


@pytest.mark.parametrize("action", ["approve", "decline"])
@pytest.mark.parametrize("state", ["stale", "expired", "declined"])
def test_cards_that_have_changed_or_ended_read_plainly(window, action, state):
    app, _, _, card = window
    engine = app.state.engine
    with engine.begin() as conn:
        if state == "expired":
            conn.execute(update(m.approval).where(m.approval.c.id == card.approval_id)
                         .values(expires_at=now() - timedelta(seconds=1)))
        elif state == "stale":
            conn.execute(update(m.item).where(m.item.c.name == "Copper Wire").values(standard_rate="29.00"))
    if state == "declined":
        checked(request(window, ("POST", "/v1/cards/{aid}/decline")), 200)
    response = request(window, ("POST", f"/v1/cards/{{aid}}/{action}"))
    checked(response, 200)
    event = response.json()["events"][0]
    if state == "stale" and action == "approve":
        assert event.get("code") == "card_changed", response.text
        assert any(e["type"] == "card" for e in response.json()["events"]), response.text
    else:
        assert event.get("status") == ("declined" if state == "stale" else state), response.text


@pytest.mark.parametrize("retry", ["replay", "changed_text", "changed_choice", "overtaken", "recorded_overtaken"])
def test_retried_messages_read_plainly(window, retry):
    from erpclaw_assistant import store

    _, client, cid, _ = window
    path = f"/v1/conversations/{cid}/messages"
    body = {"text": "hello", "client_message_id": "retry"}
    if retry in ("overtaken", "recorded_overtaken"):
        who = client.get("/v1/me").json()
        author = {"principal_id": who["principal_id"], "principal_name": who["name"]}
        stopped = store.claim(cid, "hello", client_message_id="retry", **author)
        assert stopped is not None
        store.release(stopped)
        later = store.claim(cid, "later", **author)
        assert later is not None and store.mark_answer(later)
        store.release(later)
        if retry == "recorded_overtaken":
            assert store.claim(cid, "hello", client_message_id="retry", **author).kind == "overtaken"
        response = client.post(path, json=body)
        checked(response, 200)
        assert [e.get("code") for e in response.json()["events"]] == ["message_overtaken"]
    else:
        first = client.post(path, json=body)
        checked(first, 200)
        if retry == "changed_text":
            body["text"] = "different"
        elif retry == "changed_choice":
            body["choice"] = "Q2-1"
        response = client.post(path, json=body)
        checked(response, 200 if retry == "replay" else 409)
        if retry == "replay":
            assert response.content == first.content
        else:
            assert response.json()["code"] == "client_message_id_reused"


def test_every_declared_refusal_code_was_reached_and_checked(request, refusal_code_report):
    from owner_text import runtime_coverage_skip_reason

    reason = runtime_coverage_skip_reason(request.session)
    if reason:
        pytest.skip(reason)
    # The session fixture asserts coverage at teardown, after the last producer.
