"""The ASGI app passed to the HTTP server enforces the loopback listener table."""
import os
import socket
from types import SimpleNamespace

import pytest
import typer
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from bookdiff import changes, snapshot
from erpclaw_api import app as api
from erpclaw_api.pipe import LISTENER_TABLES, ListenerApp
from erpclaw_assistant.model import ScriptedModel
from erpclaw_cli import main as cli
from erpclaw_core import administration as admin, models as m, principals as pr

NOT_FOUND = b'{"detail":"There is no such route.","code":"not_found"}'
UNAUTHENTICATED = b'{"detail":"Sign in to use the engine.","code":"unauthenticated"}'


@pytest.fixture
def env(engine, runtime_login, monkeypatch, tmp_path):
    runtime = create_engine(runtime_login)
    model = ScriptedModel([])
    with engine.begin() as conn:
        token = admin.add_principal(conn, "alice", ["view"], [])

    monkeypatch.setenv("ERPCLAW_DATABASE_URL", runtime_login)
    opened = []

    def make_engine(url):
        opened.append(url)
        return runtime

    monkeypatch.setattr("erpclaw_core.db.make_engine", make_engine)
    monkeypatch.setattr("erpclaw_assistant.muse.model_from_env", lambda: model)
    base_urls = {}

    def captured(app, **options):
        with socket.socket(fileno=os.dup(options["fd"])) as sock:
            host, port = sock.getsockname()
        base_urls[app] = f"http://{host}:{port}"
        return app, options

    def capture(**options):
        if options.get("port") is None:
            options.update(run_file=tmp_path / "run.json", tcp=True)
        served = []
        monkeypatch.setattr("uvicorn.run", lambda app, **kw: served.append(captured(app, **kw)))
        cli.serve(**options)
        assert len(served) == 1
        assert opened[-1] == runtime_login
        return served[0]

    yield SimpleNamespace(owner=engine, runtime=runtime, model=model, token=token, capture=capture,
                          client=lambda app: TestClient(app, base_url=base_urls[app]))
    runtime.dispose()


def test_serve_hands_uvicorn_a_loopback_listener(env):
    app, options = env.capture(host="127.0.0.1", port=9123)
    assert options == {"fd": options["fd"], "log_level": "warning", "ws": "none"}
    assert isinstance(app, ListenerApp)
    assert app.kind == "loopback" and app.table == LISTENER_TABLES["loopback"]
    assert app.app.state.engine is env.runtime
    assert app.app.state.assistant.model is env.model


def test_serve_refuses_other_loopback_addresses(env):
    with pytest.raises(typer.Exit) as refused:
        env.capture(host="127.0.0.2", port=9123)
    assert refused.value.exit_code == 2


def test_served_listener_hides_sessions_docs_unlisted_routes_and_head(env):
    app, _ = env.capture()
    with env.owner.begin() as conn:
        code = admin.mint_sign_in_code(conn, "alice", "owner")
    before = snapshot(env.owner)
    with env.client(app) as client:
        for body in [
            {"json": {"sign_in_code": code}},
            {"content": b'{"sign_in_code":', "headers": {"Content-Type": "application/json"}},
            {},
        ]:
            response = client.post("/v1/session", **body)
            assert response.status_code == 404 and response.content == NOT_FOUND
            assert changes(before, snapshot(env.owner)) == {}
        response = client.delete("/v1/session")
        assert response.status_code == 404 and response.content == NOT_FOUND
        for path in ["/docs", "/redoc", "/openapi.json", "/v1/nothing"]:
            response = client.get(path)
            assert response.status_code == 404 and response.content == NOT_FOUND
        for path in ["/v1/health", "/v1/me"]:
            response = client.head(path, headers={"Authorization": f"Bearer {env.token}"})
            assert response.status_code == 404
            assert response.headers["content-length"] == str(len(NOT_FOUND))
    with env.owner.connect() as conn:
        assert conn.execute(select(m.principal_sign_in.c.used_at).where(
            m.principal_sign_in.c.code_sha256 == pr.code_digest(code))).scalar_one() is None
        assert conn.execute(select(m.principal_session)).first() is None
    assert changes(before, snapshot(env.owner)) == {}


def test_served_listener_allows_health_and_bearer_identity_but_requires_a_token(env):
    app, _ = env.capture()
    with env.client(app) as client:
        response = client.get("/v1/health")
        assert response.status_code == 200 and response.json() == {"ok": True}
        response = client.get("/v1/me", headers={"Authorization": f"Bearer {env.token}"})
        assert response.status_code == 200 and response.json()["name"] == "alice"
        for method, path in LISTENER_TABLES["loopback"] - {("GET", "/v1/health")}:
            path = path.format(cid="conversation", aid="card", key="operation")
            response = client.request(method, path)
            assert response.status_code == 401 and response.content == UNAUTHENTICATED


def test_served_listener_blocks_a_registered_route_outside_its_table(env, monkeypatch):
    create_app = api.create_app
    reached = []

    def with_extra_route(*args, **kwargs):
        app = create_app(*args, **kwargs)

        @app.get("/v1/nothing")
        def extra_route():
            reached.append(True)
            return {"exposed": True}

        return app

    monkeypatch.setattr(api, "create_app", with_extra_route)
    app, _ = env.capture()
    with env.client(app) as client:
        for headers in [{}, {"Authorization": f"Bearer {env.token}"}]:
            response = client.get("/v1/nothing", headers=headers)
            assert response.status_code == 404 and response.content == NOT_FOUND
    assert reached == []
