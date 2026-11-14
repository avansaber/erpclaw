"""Portable HTTP/1.1 over an async reader/writer pair.

The platform acceptor supplies the peer identity and its mandatory connection
check. No platform APIs or listener creation live here. A reader implements
``await read(n)``; a writer implements ``write``, ``drain``, ``close`` and
``wait_closed`` as asyncio streams do. Requests are dispatched sequentially;
unambiguous, buffered requests may follow one another on a connection.

Wrap the app in ListenerApp for each listener, including a loopback ASGI server.
Routes declare openapi_extra={"erpclaw_access": "app" | "bearer" | "shared"}.
Existing unannotated routes are shared: today's API authenticates bearer tokens
itself. Future session and sign-in endpoints must declare "app"; endpoints
exclusive to token clients declare "bearer". Policy is independent of URL names.
The host wrapper permits session credentials but session validation remains the
application's responsibility. Bearer listeners require a bearer scheme and the
application validates the token and its permissions.
"""
from __future__ import annotations

import asyncio
import inspect
import re
from http import HTTPStatus
from urllib.parse import unquote_to_bytes

from starlette.responses import JSONResponse
from starlette.routing import Match

MAX_REQUEST_LINE = 8192
MAX_HEADERS = 16 * 1024
MAX_BODY = 1024 * 1024
_TOKEN = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_DECIMAL = re.compile(rb"[0-9]+\Z")
_LISTENERS = {"host": {"app", "shared"}, "cli": {"bearer", "shared"},
              "loopback": {"bearer", "shared"}}
_API_HEADERS = (
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
)


# The routes each listener serves, explicitly: a (method, path-template) pair that is not listed here is the frozen 404
# on that listener, whatever the app registers, so a route added later (a session or sign-in route above all) is
# unreachable until it is listed on purpose. HEAD is not served. The host pipe lists everything the app may call; the
# CLI pipe and the loopback listener list the bearer-token routes only.
_COMMON = (("GET", "/v1/health"), ("POST", "/v1/conversations"), ("POST", "/v1/conversations/{cid}/messages"),
           ("POST", "/v1/cards/{aid}/approve"), ("POST", "/v1/cards/{aid}/decline"), ("GET", "/v1/operations/{key}"),
           ("GET", "/v1/me"), ("GET", "/v1/cards/{aid}"),
           ("GET", "/v1/conversations/{cid}/stream"))  # the caller's own identity
LISTENER_TABLES = {
    "host": frozenset((*_COMMON, ("POST", "/v1/session"), ("DELETE", "/v1/session"))),
    "cli": frozenset(_COMMON),
    "loopback": frozenset(_COMMON),
}


def _policy(route):
    policy = (getattr(route, "openapi_extra", None) or {}).get("erpclaw_access", "shared")
    if policy not in {"app", "bearer", "shared"}:
        raise ValueError("Unknown route access policy")
    return policy


def listener_routes(routes):
    """Return allowed (method, path-template) sets for each listener.

    Only HTTP leaf routes are supported. A mounted router must be flattened by
    its owner, so a mount can never silently bypass the access table.
    """
    tables = {kind: set() for kind in _LISTENERS}
    for route in routes:
        if not getattr(route, "methods", None) or not hasattr(route, "path"):
            raise ValueError("Listener tables require HTTP leaf routes")
        policy = _policy(route)
        for kind, policies in _LISTENERS.items():
            if policy in policies:
                tables[kind].update((method, route.path) for method in route.methods)
    return tables


async def _error(send, status, code="invalid_request", detail="That HTTP request is not valid."):
    await JSONResponse({"detail": detail, "code": code}, status_code=status)({"type": "http"}, None, send)


class ListenerApp:
    """ASGI route-table gate usable by both the byte stream and TCP servers."""

    def __init__(self, app, kind, table=None, *, port=None):
        if kind not in _LISTENERS:
            raise ValueError("Unknown listener kind")
        if kind == "loopback" and (type(port) is not int or not 1 <= port <= 65535):
            raise ValueError("A loopback listener requires its run port")
        self.app, self.kind = app, kind
        self._host = f"127.0.0.1:{port}".encode("ascii") if kind == "loopback" else None
        self.table = frozenset(LISTENER_TABLES[kind] if table is None else table)
        self._all_routes = tuple(app.routes)
        self.route_table = frozenset(listener_routes(self._all_routes)[kind])
        self.routes = tuple(route for route in self._all_routes if _policy(route) in _LISTENERS[kind])

    async def __call__(self, scope, receive, send):
        scope["erpclaw_listener"] = self.kind
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        original_send = send

        async def send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {key.lower() for key, _ in headers}
                headers.extend((key, value) for key, value in _API_HEADERS if key not in present)
                message = {**message, "headers": headers}
            await original_send(message)

        if self.kind == "loopback":
            hosts = [value for key, value in scope["headers"] if key.lower() == b"host"]
            if len(hosts) != 1 or hosts[0].lower() != self._host:
                await _error(send, 421, "wrong_host", "That Host is not allowed.")
                return
        # Follow the original router's precedence, including overlapping paths.
        # An excluded specific route must not slip through an allowed wildcard.
        partial = None
        selected = None
        for route in self._all_routes:
            match, _ = route.matches(scope)
            if match == Match.FULL:
                selected = route
                break
            if match == Match.PARTIAL and partial is None:
                partial = route
        selected = selected if selected is not None else partial
        if selected is None or selected not in self.routes or (scope["method"], selected.path) not in self.table:
            await _error(send, 404, "not_found", "There is no such route.")
            return
        authorization = [value for key, value in scope["headers"] if key.lower() == b"authorization"]
        if self.kind != "host":
            value = authorization[0] if len(authorization) == 1 else b""
            scheme, _, token = value.partition(b" ")
            public_health = scope["method"] == "GET" and scope["path"] == "/v1/health" and not authorization
            if not public_health and (scheme.lower() != b"bearer" or not token.strip()):
                await _error(send, 401, "unauthenticated", "Sign in to use the engine.")
                return
        await self.app(scope, receive, send)


def served_app(engine, model, kind="loopback", *, port=None):
    """Build the API behind the explicit route table for its listener."""
    from erpclaw_api.app import create_app

    return ListenerApp(create_app(engine, model), kind, port=port)


class _Invalid(Exception):
    def __init__(self, status=400):
        self.status = status


def _closes(values):
    return any(token.strip(b" \t").lower() == b"close" for value in values for token in value.split(b","))


class _Input:
    def __init__(self, reader):
        self.reader, self.pending = reader, b""

    async def read(self, count):
        if self.pending:
            part, self.pending = self.pending[:count], self.pending[count:]
            return part
        return await self.reader.read(count)

    async def probe_disconnect(self):
        # A receive waiter may probe for EOF during a streaming response. Keep
        # at most one byte of a following request for the sequential parser.
        if not self.pending:
            self.pending = await self.reader.read(1)
        return not self.pending


async def _line(reader, limit):
    # Do not use an unbounded readline or read ahead: the identity check must
    # precede even the first header byte, and oversize input stays unread.
    data = bytearray()
    while len(data) < limit:
        part = await reader.read(1)
        if not part:
            if data:
                raise _Invalid()
            return b""
        data.extend(part)
        if part == b"\n":
            if not data.endswith(b"\r\n"):
                raise _Invalid()
            return bytes(data)
    raise _Invalid(431)


def _request_line(line):
    parts = line[:-2].split(b" ")
    if len(parts) != 3:
        raise _Invalid()
    method, target, version = parts
    if (not _TOKEN.fullmatch(method) or version != b"HTTP/1.1" or not target.startswith(b"/")
            or b"#" in target or any(c <= 32 or c >= 127 for c in target)):
        raise _Invalid()
    raw_path, _, query = target.partition(b"?")
    return {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": method.decode("ascii"), "scheme": "http",
            "path": unquote_to_bytes(raw_path).decode("utf-8", "replace"), "raw_path": raw_path,
            "query_string": query, "root_path": "", "server": None, "client": None, "state": {}}


async def _request(reader, line):
    scope = _request_line(line)
    headers, size = [], 0
    while True:
        line = await _line(reader, MAX_HEADERS - size)
        if not line:
            raise _Invalid()
        size += len(line)
        if line == b"\r\n":
            break
        key, colon, value = line[:-2].partition(b":")
        if not colon or not _TOKEN.fullmatch(key) or any(c < 32 and c != 9 or c == 127 for c in value):
            raise _Invalid()
        headers.append((key.lower(), value.strip(b" \t")))
    fields = {}
    for key, value in headers:
        fields.setdefault(key, []).append(value)
    if (len(fields.get(b"host", [])) != 1 or not fields[b"host"][0]
            or len(fields.get(b"authorization", [])) > 1 or len(fields.get(b"content-length", [])) > 1
            or b"transfer-encoding" in fields or b"upgrade" in fields):
        raise _Invalid()
    if b"expect" in fields:
        raise _Invalid(417)
    length = fields.get(b"content-length", [b"0"])[0]
    if not _DECIMAL.fullmatch(length):
        raise _Invalid()
    significant = length.lstrip(b"0") or b"0"
    if len(significant) > 7 or int(significant) > MAX_BODY:
        raise _Invalid(413)
    remaining, body = int(significant), bytearray()
    while remaining:
        part = await reader.read(min(remaining, 65536))
        if not part:
            raise _Invalid()
        body.extend(part)
        remaining -= len(part)
    scope["headers"] = headers
    close = _closes(fields.get(b"connection", []))
    return scope, bytes(body), close


class _Response:
    def __init__(self, writer, method, close):
        self.writer, self.method, self.close = writer, method, close
        self.started = self.complete = self.chunked = self.no_body = False
        self.length, self.written = None, 0

    async def send(self, message):
        if message["type"] == "http.response.start":
            if self.started or self.complete:
                raise ValueError("Response already started")
            status = message["status"]
            if not 200 <= status <= 599:
                raise ValueError("Unsupported response status")
            headers = [(key.lower(), value) for key, value in message.get("headers", [])]
            for key, value in headers:
                if not _TOKEN.fullmatch(key) or any(c < 32 and c != 9 or c == 127 for c in value):
                    raise ValueError("Invalid response header")
            lengths = [value for key, value in headers if key == b"content-length"]
            if len(lengths) > 1 or lengths and not _DECIMAL.fullmatch(lengths[0]):
                raise ValueError("Invalid response length")
            if any(key == b"transfer-encoding" for key, _ in headers):
                raise ValueError("Response framing belongs to the transport")
            self.length = int(lengths[0]) if lengths else None
            self.no_body = self.method == "HEAD" or status in {204, 304}
            self.chunked = self.length is None and not self.no_body
            self.close |= _closes(value for key, value in headers if key == b"connection")
            headers = [(key, value) for key, value in headers if key != b"connection"]
            if self.chunked:
                headers.append((b"transfer-encoding", b"chunked"))
            if self.close:
                headers.append((b"connection", b"close"))
            try:
                reason = HTTPStatus(status).phrase
            except ValueError:
                reason = "Response"
            head = f"HTTP/1.1 {status} {reason}\r\n".encode("ascii")
            head += b"".join(key + b": " + value + b"\r\n" for key, value in headers) + b"\r\n"
            self.started = True
            self.writer.write(head)
            await self.writer.drain()
        elif message["type"] == "http.response.body":
            if not self.started or self.complete:
                raise ValueError("Response body out of order")
            body = message.get("body", b"")
            if not self.no_body:
                if self.length is not None and self.written + len(body) > self.length:
                    raise ValueError("Response exceeds declared length")
                self.written += len(body)
                if body:
                    self.writer.write((f"{len(body):x}\r\n".encode() + body + b"\r\n") if self.chunked else body)
                    await self.writer.drain()
            if not message.get("more_body", False):
                if not self.no_body and self.length is not None and self.written != self.length:
                    raise ValueError("Response shorter than declared length")
                if self.chunked:
                    self.writer.write(b"0\r\n\r\n")
                    await self.writer.drain()
                self.complete = True
        else:
            raise ValueError("Unsupported ASGI response message")


async def _dispatch(app, scope, body, response, reader):
    delivered = False
    disconnected = asyncio.Event()

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        if await reader.probe_disconnect():
            return {"type": "http.disconnect"}
        # A retained byte belongs to the next request, not another body event.
        # ASGI 2.4 streaming apps also observe a closed peer via send's OSError.
        await disconnected.wait()
        return {"type": "http.disconnect"}

    try:
        await app(scope, receive, response.send)
    except OSError:
        response.close = True
    except Exception:
        response.close = True
        if not response.started:
            await _error(response.send, 500, "internal_error", "The engine hit an internal error.")
    finally:
        disconnected.set()


async def serve_connection(reader, writer, app, *, client, check):
    """Serve one accepted connection; identity lookup failure is a silent refusal.

    ``check(client)`` may be synchronous or asynchronous. It runs exactly once,
    after at most the bounded request line and before reading any header byte.
    No lifespan events are synthesized: the acceptor owns application lifespan.
    """
    reader = _Input(reader)
    try:
        try:
            line = await _line(reader, MAX_REQUEST_LINE)
        except _Invalid as exc:
            # An overlong/incomplete first line is not an answerable request.
            # A complete but malformed line can be answered after peer checking.
            if exc.status == 431:
                return
            line = b"invalid\r\n"
        if not line:
            return
        try:
            allowed = check(client)
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if allowed is not True:
                return
        except Exception:
            return
        while line:
            try:
                scope, body, close = await _request(reader, line)
            except _Invalid as exc:
                await _error(_Response(writer, "GET", True).send, exc.status)
                return
            response = _Response(writer, scope["method"], close)
            await _dispatch(app, scope, body, response, reader)
            if response.close or not response.complete:
                return
            try:
                line = await _line(reader, MAX_REQUEST_LINE)
            except _Invalid:
                return
    except (OSError, ConnectionError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
