"""Host sessions exercise PostgreSQL using the restricted runtime login."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text

from bookdiff import changes, snapshot
from erpclaw_api.app import create_app
from erpclaw_api.pipe import LISTENER_TABLES, ListenerApp
from erpclaw_assistant.model import ScriptedModel
from erpclaw_core import administration as admin, models as m, principals as pr
from erpclaw_core.common import now

RUN = "a" * 32
INVALID = {"detail": "That sign-in link is not valid any more. Ask for a new one.",
           "code": "sign_in_code_invalid"}


def header(secret):
    return {"Authorization": f"Session {secret}"}


@pytest.fixture
def env(engine, runtime_login, monkeypatch):
    runtime = create_engine(runtime_login)
    clock = [now()]
    if importlib.util.find_spec("erpclaw_api.session"):
        module = importlib.import_module("erpclaw_api.session")
        monkeypatch.setattr(module, "now", lambda: clock[0])
    with engine.begin() as conn:
        token = admin.add_principal(conn, "alice", ["view"], [])
        pid = conn.execute(select(m.principal.c.id).where(m.principal.c.name == "alice")).scalar_one()
    e = SimpleNamespace(owner=engine, runtime=runtime, clock=clock, pid=pid, token=token)

    def build(listener="host", *, run_id=None, embedded=False):
        kwargs = {"embedded_owner": embedded}
        if run_id is not None:
            kwargs["run_id"] = run_id
        app = create_app(runtime, ScriptedModel([]), **kwargs)
        return TestClient(ListenerApp(app, listener) if listener else app), app

    e.build = build
    e.client, e.app = build()
    yield e
    runtime.dispose()


def mint(e, *, run_id=None):
    with e.owner.begin() as conn:
        return admin.mint_sign_in_code(conn, "alice", "owner", run_id)


def code_row(e, code):
    with e.owner.connect() as conn:
        return dict(conn.execute(select(m.principal_sign_in).where(
            m.principal_sign_in.c.code_sha256 == pr.code_digest(code))).mappings().one())


def sessions(e):
    with e.owner.connect() as conn:
        return [dict(r) for r in conn.execute(select(m.principal_session)).mappings()]


def audits(e):
    with e.owner.connect() as conn:
        return [dict(r) for r in conn.execute(select(m.audit_log).where(
            m.audit_log.c.action.in_(["sign_in", "sign_out"]))).mappings()]


def exchange(e, code=None, *, client=None, headers=None):
    return (client or e.client).post("/v1/session", json={"sign_in_code": code or mint(e)}, headers=headers)


def signed_in(e, **kwargs):
    response = exchange(e, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()["session_secret"]


def seed_session(e, *, secret="S" * 26, **overrides):
    at = e.clock[0]
    row = dict(id="seed-session", principal_id=e.pid, secret_sha256=pr.session_digest(secret),
               created_at=at, last_seen_at=at, idle_expires_at=at + timedelta(hours=12),
               expires_at=at + timedelta(days=7), ended_at=None, ended_why=None, user_agent=None)
    row.update(overrides)
    with e.owner.begin() as conn:
        conn.execute(m.principal_session.insert().values(**row))
    return secret


def business(e):
    return {name: rows for name, rows in snapshot(e.owner).items()
            if name not in {"principal_sign_in", "principal_session", "audit_log"}}


def test_exchange_hash_lifetimes_user_agent_audit_and_contract(env, monkeypatch):
    e = env
    code = mint(e)
    with e.owner.connect() as conn:
        assert conn.execute(select(m.principal_sign_in.c.code_sha256)).scalar_one() == hashlib.sha256(
            b"erpclaw:sign-in-code:v1\0" + code.encode()).hexdigest()
    draws = []
    if importlib.util.find_spec("erpclaw_api.session"):
        module = importlib.import_module("erpclaw_api.session")
        original = module.secrets.token_urlsafe
        def secure(size):
            draws.append(size)
            return original(size)
        monkeypatch.setattr(module.secrets, "token_urlsafe", secure)
    response = exchange(e, code, headers={"User-Agent": "browser" * 50})
    assert response.status_code == 200, response.text
    body = response.json()
    example = json.loads((Path(__file__).parent / "contracts/api/session.json").read_text())
    assert body.keys() == example.keys()
    assert all(type(body[k]) is type(v) for k, v in example.items())
    assert body["name"] == "alice" and len(body["session_secret"]) == 43
    assert draws == [32]
    row, = sessions(e)
    assert row["secret_sha256"] == hashlib.sha256(
        b"erpclaw:session-secret:v1\0" + body["session_secret"].encode()).hexdigest()
    assert row["created_at"] == row["last_seen_at"] == e.clock[0]
    assert row["idle_expires_at"] == e.clock[0] + timedelta(hours=12)
    assert row["expires_at"] == e.clock[0] + timedelta(days=7)
    assert row["user_agent"] == ("browser" * 50)[:200]
    assert code_row(e, code)["used_at"] is not None
    logged, = audits(e)
    assert logged["actor"] == "alice" and logged["document_id"] == e.pid
    assert json.loads(logged["detail"]) == {"principal": "alice", "session_id": row["id"]}


def test_single_use_including_concurrent_exchange(env):
    code = mint(env)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: exchange(env, code), range(2)))
    assert sorted(r.status_code for r in results) == [200, 401]
    assert next(r for r in results if r.status_code == 401).json() == INVALID
    assert len(sessions(env)) == len(audits(env)) == 1
    assert exchange(env, code).json() == INVALID


@pytest.mark.parametrize("reason", ["unknown", "used", "expired", "disabled", "wrong_run"])
def test_five_refusals_have_identical_bytes_and_leave_no_writes(env, reason):
    code = mint(env, run_id=RUN if reason == "wrong_run" else None)
    with env.owner.begin() as conn:
        if reason == "used":
            conn.execute(m.principal_sign_in.update().values(used_at=now()))
        elif reason == "expired":
            # created_at is immutable: replace the fixture with a separately inserted expired code.
            code = "X" * 26
            conn.execute(m.principal_sign_in.insert().values(
                code_sha256=pr.code_digest(code), principal_id=env.pid,
                created_at=now() - timedelta(hours=1), expires_at=now() - timedelta(minutes=1),
                used_at=None, created_by="owner", run_id=None))
        elif reason == "disabled":
            conn.execute(m.principal.update().where(m.principal.c.id == env.pid).values(disabled=True))
        elif reason == "unknown":
            code = "Z" * 26
    before = snapshot(env.owner)
    response = exchange(env, code)
    assert response.status_code == 401 and response.content == json.dumps(INVALID, separators=(",", ":")).encode()
    assert changes(before, snapshot(env.owner)) == {}


def test_code_expiry_obeys_the_replaceable_clock(env):
    code = mint(env)
    env.clock[0] = code_row(env, code)["expires_at"]
    response = exchange(env, code)
    assert response.status_code == 401 and response.json() == INVALID
    assert code_row(env, code)["used_at"] is None


@pytest.mark.parametrize("app_run,code_run,success", [
    (RUN, RUN, True), (RUN, None, True), (None, None, True),
    (RUN, "b" * 32, False), (None, RUN, False)])
def test_code_run_binding_fails_closed(env, app_run, code_run, success):
    client, _ = env.build(run_id=app_run)
    code = mint(env, run_id=code_run)
    response = exchange(env, code, client=client)
    assert response.status_code == (200 if success else 401)
    assert (code_row(env, code)["used_at"] is not None) == success
    if not success:
        assert response.json() == INVALID and not sessions(env)


def test_throttle_eleventh_failure_engine_wide_and_resets_at_minute_boundary(env):
    good = mint(env)
    env.clock[0] = env.clock[0].replace(second=0, microsecond=0)
    for _ in range(10):
        response = exchange(env, "Z" * 26)
        assert response.status_code == 401 and response.json() == INVALID
    before = snapshot(env.owner)
    # Different clients and credentials share the engine's budget.
    second, _ = env.build()
    response = exchange(env, good, client=second)
    assert response.status_code == 429 and response.json()["code"] == "too_many_attempts"
    assert set(response.json()) == {"detail", "code"}
    assert changes(before, snapshot(env.owner)) == {}
    env.clock[0] += timedelta(seconds=59)
    assert exchange(env, good).status_code == 429
    env.clock[0] += timedelta(seconds=1)
    assert exchange(env, good).status_code == 200


@pytest.mark.parametrize("body", [{}, {"sign_in_code": "A" * 25}, {"sign_in_code": "A" * 27},
                                  {"sign_in_code": 123}, {"sign_in_code": None},
                                  {"sign_in_code": "A" * 26, "extra": "private"}])
def test_strict_body_refuses_before_code_is_spent(env, body):
    code = mint(env)
    if "extra" in body:
        body = dict(body, sign_in_code=code)
    before = snapshot(env.owner)
    response = env.client.post("/v1/session", json=body)
    assert response.status_code == 422 and response.json()["code"] == "invalid_request"
    assert code not in response.text and "private" not in response.text
    assert changes(before, snapshot(env.owner)) == {}
    assert exchange(env, code).status_code == 200


def test_already_signed_in_refuses_before_update_and_without_activity_write(env):
    secret = signed_in(env)
    code = mint(env)
    env.clock[0] += timedelta(minutes=2)
    before = snapshot(env.owner)
    response = exchange(env, code, headers=header(secret))
    assert response.status_code == 400 and response.json()["code"] == "already_signed_in"
    assert changes(before, snapshot(env.owner)) == {}
    assert exchange(env, code).status_code == 200


def test_bearer_on_exchange_is_refused_before_spending_code(env):
    code = mint(env)
    before = snapshot(env.owner)
    response = exchange(env, code, headers={"Authorization": f"Bearer {env.token}"})
    assert response.status_code == 400 and response.json()["code"] == "already_signed_in"
    assert changes(before, snapshot(env.owner)) == {}


@pytest.mark.parametrize("state", ["unknown", "ended", "idle", "absolute"])
def test_nonlive_session_does_not_prevent_a_fresh_exchange(env, state):
    options = {"ended_at": env.clock[0], "ended_why": "sign_out"} if state == "ended" else {}
    if state in {"idle", "absolute"}:
        options["idle_expires_at"] = env.clock[0]
    if state == "absolute":
        options["expires_at"] = env.clock[0]
    secret = "unknown" if state == "unknown" else seed_session(env, **options)
    assert exchange(env, headers=header(secret)).status_code == 200


def test_authentication_mode_is_chosen_by_listener(env):
    for listener in ["host", "cli", "loopback", None]:
        assert_authentication_mode(env, listener)


def assert_authentication_mode(env, listener):
    secret = "S" * 26
    if not sessions(env):
        seed_session(env, secret=secret)
    client, _ = env.build(listener)
    bearer = {"Authorization": f"Bearer {env.token}"}
    session = header(secret)
    assert client.get("/v1/me", headers=session).status_code == (200 if listener == "host" else 401)
    assert client.get("/v1/me", headers=bearer).status_code == (401 if listener == "host" else 200)
    for headers in [None,
                    [("Authorization", bearer["Authorization"]), ("Authorization", session["Authorization"])],
                    [("Authorization", session["Authorization"]), ("Authorization", bearer["Authorization"])],
                    [("Authorization", session["Authorization"]), ("Authorization", session["Authorization"])],
                    {"Authorization": bearer["Authorization"] + ", " + session["Authorization"]}]:
        response = client.get("/v1/me", headers=headers)
        assert response.status_code == 401 and response.json()["code"] == "unauthenticated"


def test_embedded_owner_only_applies_without_a_listener_and_without_a_header(env):
    for listener in [None, "host", "cli", "loopback"]:
        client, _ = env.build(listener, embedded=True)
        assert client.get("/v1/me").status_code == (200 if listener is None else 401)
        assert client.get("/v1/me", headers=header("unknown")).status_code == 401


@pytest.mark.parametrize("state", ["idle", "absolute", "ended", "disabled", "unknown"])
def test_nonlive_session_is_refused_before_route_runs(env, state):
    at = env.clock[0]
    kwargs = {"created_at": at - timedelta(days=8)}
    if state == "absolute":
        kwargs.update(expires_at=at + timedelta(seconds=1), idle_expires_at=at + timedelta(seconds=1))
    secret = seed_session(env, **kwargs)
    assert env.client.get("/v1/me", headers=header(secret)).status_code == 200
    with env.owner.begin() as conn:
        if state == "disabled":
            conn.execute(m.principal.update().where(m.principal.c.id == env.pid).values(disabled=True))
        elif state == "ended":
            conn.execute(m.principal_session.update().values(ended_at=at, ended_why="sign_out"))
    if state == "idle":
        env.clock[0] += timedelta(hours=12)
    elif state == "absolute":
        env.clock[0] += timedelta(seconds=1)
    elif state == "unknown":
        secret = "unknown"
    before = snapshot(env.owner)
    response = env.client.post("/v1/conversations", headers=header(secret))
    assert response.status_code == 401 and response.json()["code"] == "unauthenticated"
    assert changes(before, snapshot(env.owner)) == {}


def test_activity_writes_once_per_minute_over_one_hundred_calls_and_caps_idle(env):
    at = env.clock[0]
    secret = seed_session(env, idle_expires_at=at + timedelta(seconds=90), expires_at=at + timedelta(seconds=90))
    updates = []
    def count(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("UPDATE PRINCIPAL_SESSION"):
            updates.append(cursor.rowcount)
    event.listen(env.runtime, "after_cursor_execute", count)
    try:
        for elapsed in range(100):
            env.clock[0] = at + timedelta(milliseconds=elapsed * 800)
            response = env.client.get("/v1/me", headers=header(secret))
            assert response.status_code == 200, response.text
        assert updates == [1]
        row, = sessions(env)
        assert row["last_seen_at"] == at + timedelta(seconds=60)
        assert row["idle_expires_at"] == row["expires_at"] == at + timedelta(seconds=90)
    finally:
        event.remove(env.runtime, "after_cursor_execute", count)


def test_activity_extends_idle_by_twelve_hours(env):
    secret = seed_session(env)
    env.clock[0] += timedelta(minutes=1)
    assert env.client.get("/v1/me", headers=header(secret)).status_code == 200
    row, = sessions(env)
    assert row["last_seen_at"] == env.clock[0]
    assert row["idle_expires_at"] == env.clock[0] + timedelta(hours=12)


@pytest.mark.parametrize("state", ["none", "unknown", "ended", "valid", "expired", "disabled"])
def test_delete_is_idempotent_and_only_ends_a_live_session(env, state):
    options = {"ended_at": env.clock[0], "ended_why": "sign_out_all"} if state == "ended" else {}
    secret = seed_session(env, **options)
    if state == "expired":
        env.clock[0] += timedelta(hours=12)
    if state == "disabled":
        with env.owner.begin() as conn:
            conn.execute(m.principal.update().where(m.principal.c.id == env.pid).values(disabled=True))
    supplied = None if state == "none" else header("unknown" if state == "unknown" else secret)
    before = snapshot(env.owner)
    response = env.client.delete("/v1/session", headers=supplied)
    assert response.status_code == 204 and response.content == b""
    row, = sessions(env)
    if state == "valid":
        assert row["ended_at"] == env.clock[0] and row["ended_why"] == "sign_out"
        logged, = audits(env)
        assert logged["actor"] == "alice" and logged["document_id"] == env.pid
        assert json.loads(logged["detail"]) == {"principal": "alice", "session_id": row["id"]}
        assert env.client.get("/v1/me", headers=header(secret)).status_code == 401
    else:
        assert changes(before, snapshot(env.owner)) == {}
    after = snapshot(env.owner)
    assert env.client.delete("/v1/session", headers=supplied).status_code == 204
    assert changes(after, snapshot(env.owner)) == {}


def test_digest_domain_swaps_do_not_consume_codes_or_end_sessions(env):
    code = mint(env)
    # A same-length session secret passes body validation, exercising the hash domains themselves.
    secret = seed_session(env)
    before = snapshot(env.owner)
    response = env.client.get("/v1/me", headers=header(code))
    assert response.status_code == 401 and response.json()["code"] == "unauthenticated"
    response = exchange(env, secret)
    assert response.status_code == 401 and response.json() == INVALID
    assert changes(before, snapshot(env.owner)) == {}
    assert env.client.get("/v1/me", headers=header(secret)).status_code == 200
    assert exchange(env, code).status_code == 200


@pytest.mark.parametrize("listener", ["cli", "loopback"])
@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_session_routes_are_host_only_with_the_frozen_404(env, listener, method):
    client, _ = env.build(listener)
    response = client.request(method, "/v1/session", headers={"Authorization": f"Bearer {env.token}"})
    assert response.status_code == 404
    assert response.content == b'{"detail":"There is no such route.","code":"not_found"}'
    assert (method, "/v1/session") in LISTENER_TABLES["host"]
    assert all((method, "/v1/session") not in LISTENER_TABLES[k] for k in ["cli", "loopback"])
    route = next(r for r in env.app.routes if r.path == "/v1/session" and method in r.methods)
    assert route.openapi_extra == {"erpclaw_access": "app"}


def test_exchange_refusal_and_sign_out_have_empty_business_diff_and_no_secret_leaks(env, caplog):
    caplog.set_level(logging.DEBUG, logger="erpclaw_api")
    caplog.set_level(logging.DEBUG, logger="erpclaw_core")
    caplog.set_level(logging.INFO, logger="httpx")
    code = mint(env)
    before = business(env)
    response = exchange(env, code)
    assert response.status_code == 200, response.text
    answer = response.json()
    secret = answer.pop("session_secret")
    assert business(env) == before
    responses = [json.dumps(answer)]
    response = env.client.get("/v1/me", headers=header(secret))
    assert response.status_code == 200
    responses.append(response.text)
    assert business(env) == before
    response = exchange(env, code)
    assert response.status_code == 401
    responses.append(response.text)
    assert business(env) == before
    response = env.client.delete("/v1/session", headers=header(secret))
    assert response.status_code == 204
    responses.append(response.text)
    assert business(env) == before
    with env.owner.connect() as conn:
        audit_text = conn.execute(text("SELECT string_agg(row_to_json(a)::text, '') FROM audit_log a")).scalar_one()
    captured = "\n".join(responses + [caplog.text, audit_text])
    for value in [code, secret, pr.code_digest(code), pr.session_digest(secret), pr.token_digest(code), pr.token_digest(secret)]:
        assert value not in captured


def test_consumption_uses_database_wall_clock_at_the_update(env):
    code = "W" * 26
    with env.owner.begin() as conn:
        at = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        conn.execute(m.principal_sign_in.insert().values(
            code_sha256=pr.code_digest(code), principal_id=env.pid, created_at=at - timedelta(minutes=1),
            expires_at=at + timedelta(seconds=1), used_at=None, created_by="owner", run_id=None))
    waited = []
    def delay(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("UPDATE PRINCIPAL_SIGN_IN"):
            with env.owner.connect() as other:
                other.execute(text("SELECT pg_sleep(1.1)"))
            waited.append(True)
    event.listen(env.runtime, "before_cursor_execute", delay)
    try:
        response = exchange(env, code)
        assert response.status_code == 401 and response.json() == INVALID
        assert waited == [True]
        assert code_row(env, code)["used_at"] is None and not sessions(env)
    finally:
        event.remove(env.runtime, "before_cursor_execute", delay)


def test_exchange_rolls_back_consumption_and_session_if_audit_fails(env, monkeypatch):
    code = mint(env)
    before = snapshot(env.owner)
    module = importlib.import_module("erpclaw_api.session")
    def failed_audit(*args, **kwargs):
        raise RuntimeError("private internal failure")
    monkeypatch.setattr(module, "audit", failed_audit)
    client = TestClient(ListenerApp(env.app, "host"), raise_server_exceptions=False)
    response = exchange(env, code, client=client)
    assert response.status_code == 500
    assert response.json() == {"detail": "The engine hit an internal error.", "code": "internal_error"}
    assert changes(before, snapshot(env.owner)) == {}


def test_the_session_routes_do_not_exist_on_the_raw_app_and_a_code_is_not_spent(env):
    # the app served without the listener wrapper (in process, or by `erpclaw serve` over HTTP) has no session routes
    raw, _ = env.build(None)
    code = mint(env)
    for body in ({"sign_in_code": code}, {"sign_in_code": "short"}, {"extra": 1}):
        r = raw.post("/v1/session", json=body)
        assert r.status_code == 404
        assert r.json() == {"detail": "There is no such route.", "code": "not_found"}
    assert code_row(env, code)["used_at"] is None
    assert sessions(env) == []


def test_sign_out_does_not_exist_on_the_raw_app_and_a_session_stays_open(env):
    code = mint(env)
    secret = env.client.post("/v1/session", json={"sign_in_code": code}).json()["session_secret"]
    raw, _ = env.build(None)
    r = raw.delete("/v1/session", headers=header(secret))
    assert r.status_code == 404 and r.json() == {"detail": "There is no such route.", "code": "not_found"}
    assert all(row["ended_at"] is None for row in sessions(env))


@pytest.mark.parametrize("listener", ["cli", "loopback"])
def test_the_session_routes_are_404_on_the_bearer_listeners(env, listener):
    client, _ = env.build(listener)
    code = mint(env)
    r = client.post("/v1/session", json={"sign_in_code": code}, headers={"Authorization": f"Bearer {env.token}"})
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    assert code_row(env, code)["used_at"] is None


def test_the_app_that_serve_hands_to_uvicorn_has_no_session_routes(env, monkeypatch):
    import sys
    import uvicorn
    from erpclaw_cli import main as cli

    served = []
    monkeypatch.setattr(cli, "_refuse_owner_credential", lambda: None)
    monkeypatch.setattr(cli, "_books_url", lambda: "unused")
    monkeypatch.setattr("erpclaw_core.db.make_engine", lambda url: env.runtime)
    monkeypatch.setattr("erpclaw_assistant.muse.model_from_env", lambda: ScriptedModel([]))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: served.append(app))
    cli.serve(host="127.0.0.1", port=8740)
    assert len(served) == 1
    code = mint(env)
    r = TestClient(served[0]).post("/v1/session", json={"sign_in_code": code})
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    assert code_row(env, code)["used_at"] is None
