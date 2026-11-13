"""Portable HTTP transport and listener isolation, without a Windows runtime."""
from __future__ import annotations

import asyncio
import importlib
import json
import socket

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient


def pipe():
    return importlib.import_module("erpclaw_api.pipe")

RUN_PORT = 51234  # the loopback listener answers only Host 127.0.0.1:<its run port>


def listener_app(app, listener, **kw):
    return pipe().ListenerApp(app, listener, port=RUN_PORT if listener == "loopback" else None, **kw)


def gated(app, listener):
    """A fake app's gate with the table its own declarations give: these tests exercise the gate's other rules; the
    explicit reviewed tables are tested on the real app below."""
    return listener_app(app, listener, table=pipe().listener_routes(app.routes)[listener])



class Reader:
    def __init__(self, data, fragment=4096):
        self.data, self.position, self.fragment = data, 0, fragment

    async def read(self, count):
        assert count > 0
        part = self.data[self.position:self.position + min(count, self.fragment)]
        self.position += len(part)
        return part


class Writer:
    def __init__(self):
        self.parts, self.closed, self.drains = [], False, 0

    def write(self, data):
        assert not self.closed
        self.parts.append(data)

    async def drain(self):
        self.drains += 1

    def close(self):
        self.closed = True

    async def wait_closed(self):
        assert self.closed

    @property
    def data(self):
        return b"".join(self.parts)


def request(path="/echo", *, method="GET", headers=b"", body=b"", close=True):
    return (f"{method} {path} HTTP/1.1\r\nHost: engine\r\n".encode() + headers
            + (b"Connection: close\r\n" if close else b"") + b"\r\n" + body)


def responses(data):
    result = []
    while data:
        head, data = data.split(b"\r\n\r\n", 1)
        status, *lines = head.split(b"\r\n")
        headers = [tuple(line.split(b": ", 1)) for line in lines]
        fields = dict((k.lower(), v) for k, v in headers)
        if fields.get(b"transfer-encoding") == b"chunked":
            body = b""
            while True:
                size, data = data.split(b"\r\n", 1)
                count = int(size, 16)
                body += data[:count]
                assert data[count:count + 2] == b"\r\n"
                data = data[count + 2:]
                if count == 0:
                    break
        else:
            count = int(fields.get(b"content-length", b"0"))
            body, data = data[:count], data[count:]
        result.append((int(status.split()[1]), headers, body))
    return result


def refused(response, status, code="invalid_request"):
    actual, headers, raw = response
    assert actual == status
    assert dict(headers)[b"content-type"] == b"application/json"
    body = json.loads(raw)
    assert set(body) == {"detail", "code"}
    assert isinstance(body["detail"], str) and body["detail"]
    assert body["code"] == code


async def echo(scope, receive, send):
    message = await receive()
    assert message["type"] == "http.request" and message["more_body"] is False
    body = json.dumps({"method": scope["method"], "path": scope["path"],
                       "raw_path": scope["raw_path"].decode(), "query": scope["query_string"].decode(),
                       "body": message["body"].decode(), "headers": [(k.decode(), v.decode())
                                                                    for k, v in scope["headers"]]}).encode()
    assert scope["type"] == "http" and scope["http_version"] == "1.1"
    assert scope["asgi"]["version"] == "3.0" and scope["scheme"] == "http"
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                            (b"x-api-header", b"preserved"), (b"set-cookie", b"a=1"), (b"set-cookie", b"b=2")]})
    await send({"type": "http.response.body", "body": body})


async def exchange(wire, app=echo, *, check=lambda client: True, fragment=4096):
    if getattr(app, "kind", None) == "loopback":  # this listener answers only its own run port's Host
        wire = wire.replace(b"Host: engine\r\n", f"Host: 127.0.0.1:{RUN_PORT}\r\n".encode(), 1)
    reader, writer = Reader(wire, fragment), Writer()
    await pipe().serve_connection(reader, writer, app, client="peer", check=check)
    assert writer.closed
    return reader, writer


def test_request_scope_body_and_response_headers_survive_fragmented_reads():
    _, writer = asyncio.run(exchange(request("/a%20b?q=one%20two", method="POST",
                                             headers=b"Content-Length: 5\r\nX-Input: yes\r\n", body=b"hello"),
                                     fragment=1))
    [(status, headers, body)] = responses(writer.data)
    assert status == 200
    assert (b"x-api-header", b"preserved") in headers
    assert [v for k, v in headers if k == b"set-cookie"] == [b"a=1", b"b=2"]
    assert json.loads(body) == {"method": "POST", "path": "/a b", "raw_path": "/a%20b",
                                "query": "q=one%20two", "body": "hello",
                                "headers": [["host", "engine"], ["content-length", "5"],
                                            ["x-input", "yes"], ["connection", "close"]]}


def test_large_header_is_refused_before_its_remainder_is_read():
    wire = request(headers=b"X-Large: " + b"x" * 100_000 + b"\r\n")
    reader, writer = asyncio.run(exchange(wire))
    refused(responses(writer.data)[0], 431)
    assert reader.position <= len(b"GET /echo HTTP/1.1\r\n") + 16 * 1024 + 1
    assert reader.position < len(wire)


def test_many_small_headers_share_one_header_budget():
    reader, writer = asyncio.run(exchange(request(headers=b"X-Small: yes\r\n" * 3000)))
    refused(responses(writer.data)[0], 431)
    assert reader.position <= len(b"GET /echo HTTP/1.1\r\n") + 16 * 1024 + 1


@pytest.mark.parametrize("length", [b"1048577", b"9" * 5000], ids=["over-limit", "huge-decimal"])
def test_large_declared_body_is_refused_without_reading_body(length):
    wire = request(method="POST", headers=b"Content-Length: " + length + b"\r\n", body=b"x" * 100_000)
    reader, writer = asyncio.run(exchange(wire))
    refused(responses(writer.data)[0], 413)
    assert reader.position == wire.index(b"\r\n\r\n") + 4


def test_body_at_limit_is_accepted():
    body = b"x" * (1024 * 1024)
    _, writer = asyncio.run(exchange(request(method="POST", headers=b"Content-Length: 1048576\r\n", body=body)))
    assert json.loads(responses(writer.data)[0][2])["body"].encode() == body


def test_header_at_limit_is_accepted():
    prefix, suffix = b"Host: engine\r\nX-Pad: ", b"\r\nConnection: close\r\n\r\n"
    wire = b"GET /echo HTTP/1.1\r\n" + prefix + b"x" * (16384 - len(prefix + suffix)) + suffix
    _, writer = asyncio.run(exchange(wire))
    assert responses(writer.data)[0][0] == 200


@pytest.mark.parametrize("headers", [
    b"Transfer-Encoding: chunked\r\n", b"Transfer-Encoding: identity\r\n",
    b"Transfer-Encoding: chunked\r\nContent-Length: 3\r\n",
    b"Content-Length: 3\r\nContent-Length: 3\r\n", b"Content-Length: 3\r\nContent-Length: 4\r\n",
    b"Content-Length: 3, 3\r\n", b"Content-Length: +3\r\n", b"Content-Length: -1\r\n",
    b"Content-Length: nope\r\n", b"Content-Length : 3\r\n", b" folded: yes\r\n",
    b"X-Value: a\x00b\r\n", b"Bad\x80Name: value\r\n", b"No-Colon\r\n",
    b"Host: other\r\n", b"".join((b'Authori', b'zation: ', b'Bearer', b' a\r\n', b'Authori', b'zation: ', b'Session', b' b\r\n')),
    b"Upgrade: websocket\r\n",
])
def test_ambiguous_or_unsupported_framing_closes_without_dispatch(headers):
    async def no_app(*args):
        pytest.fail("an ambiguous request reached the app")

    first = request(headers=headers, body=b"bad", close=False)
    reader, writer = asyncio.run(exchange(first + request("/second"), no_app))
    refused(responses(writer.data)[0], 400)
    assert len(responses(writer.data)) == 1 and reader.position <= first.index(b"\r\n\r\n") + 4


def test_expect_continue_is_refused_without_waiting_for_body():
    reader, writer = asyncio.run(exchange(request(headers=b"Expect: 100-continue\r\nContent-Length: 4\r\n")))
    refused(responses(writer.data)[0], 417)


@pytest.mark.parametrize("wire", [b"GET /echo HTTP/1.0\r\nHost: engine\r\n\r\n",
                                   b"GET http://engine/echo HTTP/1.1\r\nHost: engine\r\n\r\n",
                                   b"GET /echo#fragment HTTP/1.1\r\nHost: engine\r\n\r\n",
                                   b"GET /echo HTTP/1.1\r\n\r\n",
                                   b"GET /echo HTTP/1.1\nHost: engine\n\n"])
def test_malformed_request_never_reaches_app(wire):
    async def no_app(*args):
        pytest.fail("a malformed request reached the app")

    _, writer = asyncio.run(exchange(wire, no_app))
    refused(responses(writer.data)[0], 400)


def test_overlong_first_line_closes_without_consuming_it():
    wire = b"GET /" + b"x" * 100_000 + b" HTTP/1.1\r\n\r\n"
    reader, writer = asyncio.run(exchange(wire))
    assert reader.position <= 8193 and writer.data == b""


@pytest.mark.parametrize("wire", [b"GET /echo HTTP/1.1\r\nHost: engine\r\n",
                                   request(method="POST", headers=b"Content-Length: 4\r\n", body=b"ab")])
def test_truncated_request_is_refused_without_dispatch(wire):
    async def no_app(*args):
        pytest.fail("a truncated request reached the app")

    _, writer = asyncio.run(exchange(wire, no_app))
    refused(responses(writer.data)[0], 400)


@pytest.mark.parametrize("decision", [False, True])
def test_connection_check_runs_once_before_any_header_is_read(decision):
    async def run():
        first = request(close=False)
        reader, writer, calls = Reader(first + request("/second")), Writer(), []

        async def check(client):
            calls.append(client)
            assert reader.position == len(b"GET /echo HTTP/1.1\r\n")
            return decision

        await pipe().serve_connection(reader, writer, echo, client="identity", check=check)
        assert calls == ["identity"] and writer.closed
        if decision:
            assert len(responses(writer.data)) == 2
        else:
            assert writer.data == b"" and reader.position == len(b"GET /echo HTTP/1.1\r\n")

    asyncio.run(run())


def test_check_exception_closes_without_reading_headers_or_writing():
    def check(client):
        raise RuntimeError("identity lookup unavailable")

    reader, writer = asyncio.run(exchange(request(), check=check))
    assert writer.data == b"" and reader.position == len(b"GET /echo HTTP/1.1\r\n")


def test_two_buffered_requests_and_bodies_are_answered_in_order():
    wire = (request("/first", method="POST", headers=b"Content-Length: 3\r\n", body=b"one", close=False)
            + request("/second", method="POST", headers=b"Content-Length: 3\r\n", body=b"two"))
    _, writer = asyncio.run(exchange(wire))
    assert [(json.loads(body)["path"], json.loads(body)["body"]) for _, _, body in responses(writer.data)] == [
        ("/first", "one"), ("/second", "two")]


@pytest.mark.parametrize("connection", [b"close", b"CLOSE", b"keep-alive,\tclose\t, other"])
def test_connection_close_does_not_dispatch_buffered_next_request(connection):
    first = request("/first", headers=b"Connection: " + connection + b"\r\n", close=False)
    reader, writer = asyncio.run(exchange(first + request("/second")))
    assert len(responses(writer.data)) == 1 and reader.position == len(first)


def test_socket_pair_supports_keepalive_with_second_request_sent_after_first_response():
    async def run():
        server_socket, client_socket = socket.socketpair()
        server_reader, server_writer = await asyncio.open_connection(sock=server_socket)
        client_reader, client_writer = await asyncio.open_connection(sock=client_socket)
        task = asyncio.create_task(pipe().serve_connection(server_reader, server_writer, echo,
                                                          client="peer", check=lambda client: True))
        try:
            for path, close in [("/first", False), ("/second", True)]:
                client_writer.write(request(path, close=close))
                await client_writer.drain()
                head = await asyncio.wait_for(client_reader.readuntil(b"\r\n\r\n"), 2)
                length = int(next(line.split(b": ")[1] for line in head.split(b"\r\n")
                                  if line.startswith(b"content-length:")))
                body = await asyncio.wait_for(client_reader.readexactly(length), 2)
                assert json.loads(body)["path"] == path
            assert await asyncio.wait_for(client_reader.read(), 2) == b""
            await asyncio.wait_for(task, 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            client_writer.close()
            await client_writer.wait_closed()

    asyncio.run(run())


def test_event_stream_flushes_each_chunk_before_generator_continues():
    async def run():
        writer = Writer()

        async def events():
            yield b"data: first\n\n"
            assert b"data: first\n\n" in writer.data and writer.drains >= 2
            assert b"data: second\n\n" not in writer.data
            yield b"data: second\n\n"

        app = FastAPI()

        @app.get("/events")
        async def stream():
            return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

        await pipe().serve_connection(Reader(request("/events")), writer, app,
                                      client="peer", check=lambda client: True)
        [(status, headers, body)] = responses(writer.data)
        assert status == 200 and body == b"data: first\n\ndata: second\n\n"
        assert (b"cache-control", b"no-cache") in headers
        assert (b"transfer-encoding", b"chunked") in headers
        assert writer.data.endswith(b"0\r\n\r\n")

    asyncio.run(run())


def test_asgi_receive_reports_a_disconnected_peer():
    async def app(scope, receive, send):
        assert (await receive())["type"] == "http.request"
        message = await asyncio.wait_for(receive(), 0.1)
        assert message == {"type": "http.disconnect"}
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"0")]})
        await send({"type": "http.response.body"})

    _, writer = asyncio.run(exchange(request(), app))
    assert responses(writer.data)[0][0] == 200


def test_disconnect_probe_preserves_the_next_pipelined_request():
    observed = []

    async def app(scope, receive, send):
        await receive()
        try:
            message = await asyncio.wait_for(receive(), 0.02)
        except TimeoutError:
            message = None
        observed.append((scope["path"], message))
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"0")]})
        await send({"type": "http.response.body"})

    _, writer = asyncio.run(exchange(request("/first", close=False) + request("/second"), app))
    assert len(responses(writer.data)) == 2
    assert observed == [("/first", None), ("/second", {"type": "http.disconnect"})]


@pytest.mark.parametrize("method, status", [("HEAD", 200), ("GET", 204), ("GET", 304)])
def test_head_and_bodyless_statuses_do_not_write_body_or_chunk_framing(method, status):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"discard"})

    _, writer = asyncio.run(exchange(request(method=method), app))
    head, body = writer.data.split(b"\r\n\r\n", 1)
    assert body == b"" and b"transfer-encoding" not in head.lower()


@pytest.mark.parametrize("after_response", [False, True])
def test_application_failure_preserves_a_started_response_and_closes(after_response):
    async def app(scope, receive, send):
        if after_response:
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-length", b"5")]})
            await send({"type": "http.response.body", "body": b"saved"})
        raise RuntimeError("private exception")

    _, writer = asyncio.run(exchange(request(), app))
    if after_response:
        assert responses(writer.data)[0][2] == b"saved" and b"500" not in writer.data
    else:
        refused(responses(writer.data)[0], 500, "internal_error")
        assert b"private exception" not in writer.data


def fake_app():
    app = FastAPI(redirect_slashes=False, docs_url=None, redoc_url=None, openapi_url=None)

    async def app_only(request: Request):
        return {"route": "host"}

    async def shared(request: Request):
        credential = request.headers.get("authorization")
        if credential not in {" ".join(('Bearer', 'valid')), "Session valid"}:
            return JSONResponse({"detail": "Sign in to use the engine.", "code": "unauthenticated"}, status_code=401)
        return {"route": "shared", "credential": credential}

    async def bearer_only(request: Request):
        return {"route": "bearer"}

    for path in ["/v1/session", "/v1/sign-in/code", "/v1/unrelated-app-path"]:
        app.add_api_route(path, app_only, methods=["POST"], openapi_extra={"erpclaw_access": "app"})
    app.add_api_route("/v1/cards/{card_id}", shared, methods=["GET"], openapi_extra={"erpclaw_access": "shared"})
    app.add_api_route("/v1/token-exchange", bearer_only, methods=["POST"],
                      openapi_extra={"erpclaw_access": "bearer"})
    return app


def test_listener_tables_partition_explicit_route_policy_without_path_conventions():
    tables = pipe().listener_routes(fake_app().routes)
    shared = {("GET", "/v1/cards/{card_id}")}
    host = {("POST", p) for p in ["/v1/session", "/v1/sign-in/code", "/v1/unrelated-app-path"]}
    bearer = {("POST", "/v1/token-exchange")}
    assert tables == {"host": shared | host, "cli": shared | bearer, "loopback": shared | bearer}


@pytest.mark.parametrize("listener, path, method", [
    ("cli", "/v1/session", "POST"), ("cli", "/v1/sign-in/code", "POST"),
    ("cli", "/v1/unrelated-app-path", "POST"), ("loopback", "/v1/session", "POST"),
    ("loopback", "/v1/sign-in/code", "POST"), ("host", "/v1/token-exchange", "POST"),
    ("cli", "/v1/session", "GET"),
])
def test_listener_table_actually_blocks_excluded_routes(listener, path, method):
    app = gated(fake_app(), listener)
    _, writer = asyncio.run(exchange(request(path, method=method, headers=b"".join((b'Authori', b'zation: ', b'Bearer', b' valid\r\n'))), app))
    refused(responses(writer.data)[0], 404, "not_found")


@pytest.mark.parametrize("listener", ["host", "cli", "loopback"])
def test_session_authorization_is_host_only_even_on_shared_routes(listener):
    app = gated(fake_app(), listener)
    _, writer = asyncio.run(exchange(request("/v1/cards/card", headers=b"".join((b'Authori', b'zation: ', b'Session', b' valid\r\n'))), app))
    response = responses(writer.data)[0]
    if listener == "host":
        assert response[0] == 200 and json.loads(response[2])["credential"] == "Session valid"
    else:
        refused(response, 401, "unauthenticated")


@pytest.mark.parametrize("listener", ["cli", "loopback"])
@pytest.mark.parametrize("credential", [b"", b"".join((b'Authori', b'zation: ', b'Bearer', b' invalid\r\n')), b"".join((b'Authori', b'zation: Cookie valid\r\n'))])
def test_bearer_listeners_require_a_valid_bearer(listener, credential):
    app = gated(fake_app(), listener)
    _, writer = asyncio.run(exchange(request("/v1/cards/card", headers=credential), app))
    refused(responses(writer.data)[0], 401, "unauthenticated")


@pytest.mark.parametrize("listener", ["cli", "loopback"])
def test_bearer_listeners_forward_valid_token_on_template_route(listener):
    app = gated(fake_app(), listener)
    _, writer = asyncio.run(exchange(request("/v1/cards/card", headers=b"".join((b'Authori', b'zation: ', b'Bearer', b' valid\r\n'))), app))
    assert responses(writer.data)[0][0] == 200


def test_loopback_wrapper_is_also_an_asgi_app_for_existing_http_servers():
    client = TestClient(gated(fake_app(), "loopback"), base_url=f"http://127.0.0.1:{RUN_PORT}")
    response = client.get("/v1/cards/card", headers={"Authorization": "Session valid"})
    assert response.status_code == 401 and response.json()["code"] == "unauthenticated"


def test_invalid_listener_or_route_policy_fails_closed():
    app = fake_app()
    with pytest.raises(ValueError):
        pipe().ListenerApp(app, "other")
    app.routes[0].openapi_extra["erpclaw_access"] = "typo"
    with pytest.raises(ValueError):
        pipe().listener_routes(app.routes)


def test_real_app_route_inventory_and_health_are_preserved(engine):
    from erpclaw_api.app import create_app
    from erpclaw_assistant.model import ScriptedModel

    app = create_app(engine, ScriptedModel([]))
    tables = pipe().listener_routes(app.routes)
    expected = {("GET", "/v1/health"), ("POST", "/v1/conversations"),
                ("POST", "/v1/conversations/{cid}/messages"), ("POST", "/v1/cards/{aid}/approve"),
                ("POST", "/v1/cards/{aid}/decline"), ("GET", "/v1/operations/{key}"),
                # the caller's own identity: served on every listener, the bearer ones included
                ("GET", "/v1/me"),}
    for listener in ["host", "cli", "loopback"]:
        # EXACT, not a subset: a route added later (a session or sign-in route above all) fails this test until the
        # access table below is reviewed, because an unannotated route is shared and would reach the CLI listener
        reviewed = expected | ({("POST", "/v1/session"), ("DELETE", "/v1/session")} if listener == "host" else set())
        assert reviewed == tables[listener]
        wrapped = listener_app(app, listener)
        _, writer = asyncio.run(exchange(request("/v1/health"), wrapped))
        assert responses(writer.data)[0][0] == 200
        assert json.loads(responses(writer.data)[0][2]) == {"ok": True}
        _, writer = asyncio.run(exchange(request("/v1/conversations", method="POST",
                                                 headers=b"".join((b'Authori', b'zation: ', b'Session', b' valid\r\n'))), wrapped))
        refused(responses(writer.data)[0], 401, "unauthenticated")


def test_a_route_added_without_an_access_decision_changes_the_inventory_and_is_caught():
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/v1/health")
    def health():
        return {"ok": True}

    before = pipe().listener_routes(app.routes)

    @app.post("/v1/session")  # a session route that forgot to declare itself app-only
    def session():
        return {}

    after = pipe().listener_routes(app.routes)
    assert before["cli"] != after["cli"] and ("POST", "/v1/session") in after["cli"]  # the gap the inventory test detects

    @app.post("/v1/session/declared", openapi_extra={"erpclaw_access": "app"})
    def declared():
        return {}

    final = pipe().listener_routes(app.routes)
    assert ("POST", "/v1/session/declared") in final["host"]
    assert ("POST", "/v1/session/declared") not in final["cli"] and ("POST", "/v1/session/declared") not in final["loopback"]


def test_a_route_the_app_adds_but_the_table_does_not_list_is_404_on_every_listener():
    from fastapi import FastAPI

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/v1/health")
    def health():
        return {"ok": True}

    @app.post("/v1/unreviewed")  # added to the app, never listed in any table, and never declared app-only
    def session():
        return {"leaked": True}

    for listener in ["host", "cli", "loopback"]:
        gate = listener_app(app, listener)
        _, writer = asyncio.run(exchange(request("/v1/unreviewed", method="POST", headers=b"".join((b'Authori', b'zation: ', b'Bearer', b' x\r\n'))), gate))
        refused(responses(writer.data)[0], 404, "not_found")
        _, writer = asyncio.run(exchange(request("/v1/health"), gate))
        assert responses(writer.data)[0][0] == 200  # a listed route is still served


def test_documentation_routes_and_head_are_404_on_every_listener_of_the_real_app(engine):
    from erpclaw_api.app import create_app
    from erpclaw_assistant.model import ScriptedModel

    app = create_app(engine, ScriptedModel([]))
    for listener in ["host", "cli", "loopback"]:
        gate = listener_app(app, listener)
        for method, path in [("GET", "/docs"), ("GET", "/redoc"), ("GET", "/openapi.json"), ("GET", "/docs/oauth2-redirect"),
                             ("HEAD", "/openapi.json"), ("HEAD", "/v1/health")]:
            _, writer = asyncio.run(exchange(request(path, method=method, headers=b"".join((b'Authori', b'zation: ', b'Bearer', b' x\r\n'))), gate))
            if method == "HEAD":  # a HEAD answer has no body to parse
                assert responses(writer.data)[0][0] == 404
            else:
                refused(responses(writer.data)[0], 404, "not_found")


def test_the_reviewed_tables_list_only_what_each_listener_may_serve():
    tables = pipe().LISTENER_TABLES
    assert tables["cli"] == tables["loopback"]
    assert tables["host"] == tables["cli"] | {("POST", "/v1/session"), ("DELETE", "/v1/session")}
    assert not any("session" in path or "sign" in path for listener in ("cli", "loopback") for _, path in tables[listener])
