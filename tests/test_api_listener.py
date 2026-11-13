"""Loopback binding, request authority, and headers on the served API."""
from __future__ import annotations

import asyncio
import base64
import errno
import logging
import os
import random
import secrets
import socket
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from starlette.routing import WebSocketRoute
from typer.testing import CliRunner

from erpclaw_api.app import create_app
from erpclaw_api.pipe import LISTENER_TABLES, ListenerApp, served_app
from erpclaw_assistant.model import ScriptedModel
from erpclaw_cli import main as cli
from erpclaw_core import administration as admin

REMOTE = "ERPClaw serves this machine only in this version; a remote engine is a later design."
WRONG_HOST = b'{"detail":"That Host is not allowed.","code":"wrong_host"}'
UNAUTHENTICATED = b'{"detail":"Sign in to use the engine.","code":"unauthenticated"}'
SECURITY_HEADERS = {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cross-origin-resource-policy": "same-origin",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
}


def assert_headers(response, *, overrides=None):
    for name, value in (SECURITY_HEADERS | (overrides or {})).items():
        assert response.headers.get_list(name) == [value]
    assert not any(name.lower().startswith("access-control-") for name in response.headers)


@contextmanager
def reserved_port(seed=17):
    generator = random.Random(seed)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        for _ in range(32):
            port = generator.randint(49152, 65471)
            try:
                sock.bind(("127.0.0.1", port))
                break
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:
                    raise
        else:
            pytest.fail("Could not reserve a test listener")
        yield port


@pytest.fixture
def env(engine, runtime_login, monkeypatch, tmp_path):
    runtime = create_engine(runtime_login)
    model = ScriptedModel([])
    with engine.begin() as conn:
        token = admin.add_principal(conn, "alice", ["view"], [])
    monkeypatch.setenv("ERPCLAW_DATABASE_URL", runtime_login)
    monkeypatch.setattr("erpclaw_core.db.make_engine", lambda url: runtime)
    monkeypatch.setattr("erpclaw_assistant.muse.model_from_env", lambda: model)
    starts = []
    real_run = uvicorn.run

    def capture(app, **options):
        start = SimpleNamespace(app=app, options=options, port=options.get("port", 8740))
        if "fd" in options:
            with socket.socket(fileno=os.dup(options["fd"])) as bound:
                start.family = bound.family
                start.socket_type = bound.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
                start.address = bound.getsockname()
                start.port = start.address[1]
                start.reuse = bound.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)
                start.exclusive = (bound.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE)
                                   if hasattr(socket, "SO_EXCLUSIVEADDRUSE") else None)
        starts.append(start)

    monkeypatch.setattr("uvicorn.run", capture)

    def start(**options):
        if options.get("port") is None:
            options.update(run_file=tmp_path / "run.json", tcp=True)
        cli.serve(**options)
        return starts[-1]

    yield SimpleNamespace(runtime=runtime, model=model, token=token, starts=starts, start=start, real_run=real_run)
    runtime.dispose()


def client_for(start):
    return TestClient(start.app, base_url=f"http://127.0.0.1:{start.port}")


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "::1", "localhost", "engine.example", "192.168.1.2", "127.0.0.2"])
def test_remote_host_is_refused_before_opening_anything(monkeypatch, host):
    def unopened(*args, **kwargs):
        pytest.fail("A refused host opened a resource or started the server")

    monkeypatch.setattr(socket, "socket", unopened)
    monkeypatch.setattr("uvicorn.run", unopened)
    monkeypatch.setattr("erpclaw_core.db.make_engine", unopened)
    result = CliRunner().invoke(cli.app, ["serve", "--host", host])
    assert result.exit_code == 2
    assert result.output.strip() == REMOTE

    monkeypatch.setenv("ERPCLAW_MIGRATION_URL", "postgresql+psycopg://owner@/books_test")
    result = CliRunner().invoke(cli.app, ["serve", "--host", host])
    assert result.exit_code == 1 and "owner credential" in result.output
    assert REMOTE not in result.output


def test_server_receives_an_exclusive_ipv4_loopback_socket(env):
    start = env.start()
    assert "fd" in start.options
    assert start.family == socket.AF_INET and start.address == ("127.0.0.1", start.port)
    assert start.socket_type == socket.SOCK_STREAM
    assert start.reuse == 0
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        assert start.exclusive == 1
    assert start.options == {"fd": start.options["fd"], "log_level": "warning", "ws": "none"}
    with pytest.raises(OSError):
        os.fstat(start.options["fd"])


def test_served_application_has_no_websocket_routes(env):
    app = create_app(env.runtime, env.model)
    seen = set()

    def walk(app):
        if id(app) in seen:
            return
        seen.add(id(app))
        assert not isinstance(app, WebSocketRoute), "A WebSocket route needs the Host check first."
        for route in getattr(app, "routes", ()):
            walk(route)
        wrapped = getattr(app, "app", None)
        if wrapped is not None:
            walk(wrapped)

    # Check before ListenerApp builds its HTTP-only table, so a new WebSocket route
    # reports the missing Host check rather than a generic table-construction error.
    walk(app)
    walk(env.start().app)


def test_serve_disables_websocket_support(env):
    start = env.start()
    assert start.options.get("ws") == "none"


def test_real_server_upgrade_and_duplicate_host_responses(env, monkeypatch, tmp_path):
    responses = []

    async def exchange(port, hosts, *, upgrade=False):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            headers = b"".join(b"Host: " + host + b"\r\n" for host in hosts)
            if upgrade:
                headers += (b"Connection: Upgrade\r\nUpgrade: websocket\r\n"
                            b"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: "
                            + base64.b64encode(bytes(range(16))) + b"\r\n")
            writer.write(b"GET /v1/health HTTP/1.1\r\n" + headers + b"\r\n")
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
            status, *fields = head[:-4].split(b"\r\n")
            headers = [(name, value.strip()) for name, value in (field.split(b":", 1) for field in fields)]
            response = httpx.Response(int(status.split()[1]), headers=headers)
            assert response.status_code != 101, "WebSocket upgrades must never be accepted."
            if "content-length" in response.headers:
                body = await reader.readexactly(int(response.headers["content-length"]))
            else:
                assert response.headers["connection"] == "close"
                body = await reader.read()
            response = httpx.Response(response.status_code, headers=headers, content=body)
            responses.append(response)
        finally:
            writer.close()
            await writer.wait_closed()

    async def probe(server):
        port = server.servers[0].sockets[0].getsockname()[1]
        host = f"127.0.0.1:{port}".encode("ascii")
        try:
            await asyncio.wait_for(exchange(port, [host], upgrade=True), timeout=5)
            await asyncio.wait_for(exchange(port, [b"wrong.example"], upgrade=True), timeout=5)
            await asyncio.wait_for(exchange(port, [host, host]), timeout=5)
        finally:
            server.should_exit = True

    monkeypatch.setattr(uvicorn.Server, "main_loop", probe)
    monkeypatch.setattr(uvicorn, "run", env.real_run)
    cli.serve(run_file=tmp_path / "run.json", tcp=True)

    correct, wrong, duplicate = responses
    # With ws="none", upgrade attempts remain HTTP and pass through the Host check.
    assert correct.status_code == 200 and correct.content == b'{"ok":true}'
    assert_headers(correct)
    assert wrong.status_code == 421 and wrong.content == WRONG_HOST
    assert_headers(wrong)
    # Stated exception: uvicorn's HTTP parser rejects duplicate Host before the app runs.
    assert duplicate.status_code == 400 and duplicate.content == b"Invalid HTTP request received."
    assert not (SECURITY_HEADERS.keys() & duplicate.headers.keys())


def test_port_draw_is_seedable_and_stays_inside_the_reserved_range():
    expected = random.Random(23)
    actual = random.Random(23)
    draws = [cli._draw_port(actual) for _ in range(10000)]
    assert draws == [expected.randint(49152, 65471) for _ in draws]
    assert min(draws) == 49152 and max(draws) == 65471


def test_each_default_start_uses_the_supplied_random_draw(env, monkeypatch):
    ports = []
    for seed in [31, 57]:
        monkeypatch.setattr(secrets, "SystemRandom", lambda: random.Random(seed))
        start = env.start()
        assert start.port == random.Random(seed).randint(49152, 65471)
        ports.append(start.port)
    assert ports[0] != ports[1]


def test_default_port_redraws_after_a_real_bind_collision(env, monkeypatch):
    with reserved_port() as taken:
        with reserved_port(29) as free:
            pass
        draws = iter([taken, free])
        generator = SimpleNamespace(randint=lambda low, high: next(draws))
        monkeypatch.setattr(secrets, "SystemRandom", lambda: generator)
        start = env.start()
        assert start.port == free
        assert list(draws) == []


def test_default_port_stops_after_32_collisions_without_printing_a_port(env, monkeypatch, tmp_path):
    with reserved_port() as taken:
        draws = []

        def draw(low, high):
            draws.append((low, high))
            return taken

        monkeypatch.setattr(secrets, "SystemRandom", lambda: SimpleNamespace(randint=draw))
        result = CliRunner().invoke(cli.app, ["serve", "--run-file", str(tmp_path / "run.json"), "--tcp"])
    assert result.exit_code == 1
    assert len(draws) == 32 and set(draws) == {(49152, 65471)}
    assert result.output.strip() == "The engine could not open a loopback listener."
    assert str(taken) not in result.output and not env.starts


def test_explicit_port_binds_exactly_that_port(env):
    with reserved_port() as port:
        pass
    result = CliRunner().invoke(cli.app, ["serve", "--port", str(port)])
    assert result.exit_code == 0, result.output
    assert env.starts[-1].port == port
    assert "fd" in env.starts[-1].options


def test_explicit_taken_port_is_refused_without_redrawing(env, monkeypatch):
    def no_draw(*args):
        pytest.fail("An explicit port must not draw another port")

    monkeypatch.setattr(secrets, "SystemRandom", no_draw)
    with reserved_port() as taken:
        result = CliRunner().invoke(cli.app, ["serve", "--port", str(taken)])
    assert result.exit_code == 1 and not env.starts
    assert result.output.strip() == "The engine could not open a loopback listener."


def test_wrong_host_precedes_routing_authentication_and_body_reads(env):
    start = env.start()
    good = f"127.0.0.1:{start.port}".encode()
    host_values = [[], [(b"host", good), (b"host", good)]]
    host_values += [[(b"host", value)] for value in [
        b"", b" " + good, good + b", " + good,
        f"localhost:{start.port}".encode(), f"[::1]:{start.port}".encode(), b"127.0.0.1",
        f"127.0.0.1:{start.port + 1}".encode(), good + b".", good + b" ",
        f"127.0.0.1.:{start.port}".encode(), f"engine.example:{start.port}".encode(),
    ]]

    async def run():
        for path in ["/v1/conversations", "/v1/health", "/v1/nothing"]:
            for authenticated in [False, True]:
                for hosts in host_values:
                    headers = hosts + ([(b"authorization", f"Bearer {env.token}".encode())]
                                       if authenticated else [])
                    scope = {"type": "http", "method": "GET" if path != "/v1/conversations" else "POST",
                             "path": path, "headers": headers, "query_string": b""}
                    messages = []

                    async def unread():
                        pytest.fail("Wrong Host consumed the body")

                    async def send(message):
                        messages.append(message)

                    await start.app(scope, unread, send)
                    assert messages[0]["status"] == 421
                    assert b"".join(m.get("body", b"") for m in messages) == WRONG_HOST
    asyncio.run(run())
    with client_for(start) as client:
        assert client.get("/v1/health").status_code == 200
        assert client.get("/v1/me", headers={"Authorization": f"Bearer {env.token}"}).status_code == 200


def test_security_headers_cover_every_loopback_route_and_error(env):
    start = env.start()
    statuses = set()
    with client_for(start) as client:
        for method, template in sorted(LISTENER_TABLES["loopback"]):
            path = template.format(cid="missing", aid="missing", key="missing")
            for headers, body in [({}, {}), ({"Authorization": f"Bearer {env.token}"}, {"extra": True}),
                                  ({"Host": "elsewhere"}, {})]:
                response = client.request(method, path, headers=headers, **({"json": body} if method == "POST" else {}))
                statuses.add(response.status_code)
                assert_headers(response)
        response = client.get("/v1/nothing")
        statuses.add(response.status_code)
        assert_headers(response)
    assert {200, 401, 404, 421, 422} <= statuses


def test_every_listener_adds_headers_and_preserves_route_values(env):
    for kind in ["host", "cli", "loopback"]:
        app = create_app(env.runtime, env.model)
        # Make the route change on a separate app before taking the wrapper's route snapshot.
        @app.get("/v1/test-file")
        def file_response():
            return JSONResponse({}, headers={name.upper(): "private" for name in SECURITY_HEADERS})

        table = LISTENER_TABLES[kind] | {("GET", "/v1/test-file")}
        wrapped = ListenerApp(app, kind, table=table, port=51234)
        base_url = "http://127.0.0.1:51234" if kind == "loopback" else "http://pipe"
        with TestClient(wrapped, base_url=base_url) as client:
            assert_headers(client.get("/v1/health"))
            if kind != "loopback":
                request = client.build_request("GET", "/v1/health")
                request.headers.pop("host")
                response = client.send(request)
                assert response.status_code == 200
                assert_headers(response)
            assert_headers(client.get("/v1/nothing"))
            response = client.get("/v1/me")
            assert response.status_code == 401
            assert_headers(response)
            response = client.get("/v1/test-file", headers={"Authorization": f"Bearer {env.token}"})
            assert response.status_code == 200
            assert_headers(response, overrides=dict.fromkeys(SECURITY_HEADERS, "private"))


def test_loopback_requires_a_run_port(env):
    app = create_app(env.runtime, env.model)
    with pytest.raises(ValueError, match="port"):
        ListenerApp(app, "loopback")
    with pytest.raises(ValueError, match="port"):
        served_app(env.runtime, env.model)


def test_preflight_gets_a_404_with_security_headers_and_no_cors(env):
    start = env.start()
    with client_for(start) as client:
        for origin in ["https://elsewhere.example", f"http://127.0.0.1:{start.port}", "null"]:
            for method, path in LISTENER_TABLES["loopback"]:
                response = client.options(path.format(cid="missing", aid="missing", key="missing"), headers={
                    "Origin": origin, "Access-Control-Request-Method": method})
                assert response.status_code == 404
                assert_headers(response)


def test_served_app_ignores_cookies_and_refuses_session_credentials(env):
    start = env.start()
    with client_for(start) as client:
        for headers in [{"Cookie": "session=x"}, {"Authorization": "Session x"},
                        {"Cookie": "session=x", "Authorization": "Session x"}]:
            response = client.get("/v1/me", headers=headers)
            assert response.status_code == 401 and response.content == UNAUTHENTICATED
            assert_headers(response)
        response = client.get("/v1/me", headers={"Cookie": "session=x", "Authorization": f"Bearer {env.token}"})
        assert response.status_code == 200
        assert_headers(response)


def test_start_never_prints_or_logs_the_run_port(env, caplog, capsys, monkeypatch):
    capture = uvicorn.run

    async def stop_after_start(server):
        server.should_exit = True

    def run(app, **options):
        capture(app, **options)
        env.real_run(app, **options)

    monkeypatch.setattr(uvicorn.Server, "main_loop", stop_after_start)
    monkeypatch.setattr(uvicorn, "run", run)
    caplog.set_level(logging.DEBUG)
    start = env.start()
    output = capsys.readouterr()
    assert str(start.port) not in output.out + output.err
    assert all(str(start.port) not in record.getMessage() for record in caplog.records)
    assert start.options.get("log_level") == "warning"
