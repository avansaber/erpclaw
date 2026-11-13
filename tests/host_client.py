"""Blocking desktop-host client over the engine's portable byte-stream server.

The Unix socket substitutes for the Windows acceptor only. SID and release
image checks are injectable callbacks, not platform checks implemented here.
The host-check callable is out of band: the engine has no such endpoint yet.
"""
from __future__ import annotations

import asyncio
import hmac
import http.client
import json as jsonlib
import secrets
import socket
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from bookdiff import changes, snapshot
from erpclaw_api.app import create_app
from erpclaw_api.pipe import ListenerApp, serve_connection
from erpclaw_assistant.model import ScriptedModel
from erpclaw_core import administration as admin, principals as pr
from erpclaw_core.common import company_ctx
from erpclaw_core.setup import create_company

SESSION_TABLES = frozenset({"principal_session", "principal_sign_in"})
_DEFAULT = object()


class Concealed(str):
    """A credential that behaves as text but stays private in container and local reprs."""

    def __repr__(self):
        return "<credential>"


def hmac_check(run_secret: bytes, run_id: str, challenge: bytes) -> bytes:
    """HMAC-SHA256(label NUL, length-prefixed UTF-8 run id, challenge)."""
    __tracebackhide__ = True
    identity = run_id.encode("utf-8")
    message = b"erpclaw-host-check-v1\0" + len(identity).to_bytes(4, "big") + identity + challenge
    return hmac.digest(run_secret, message, "sha256")


class ClientError(OSError):
    """A credential-free client failure; also compatible with transport-error callers."""


class ServerRefused(ClientError):
    """No credentials may be minted or sent to this unverified server."""


def _fail(method, path, status, kind, error_class=ClientError):
    """Keep a visible reporting frame containing only safe diagnostic metadata."""
    status_text = "" if status is None else f": HTTP {status}"
    raise error_class(f"{method} {path}{status_text} ({kind})") from None


def _close(connection, response):
    """Release both resources even if closing either fails; retain only its error class."""
    __tracebackhide__ = True
    kind = None
    for resource in (response, connection):
        if resource is not None:
            try:
                resource.close()
            except Exception as exc:
                kind = type(exc).__name__
    return kind


def check_client(client, *, client_sid_check=None, client_image_check=None):
    """Portable acceptor hook; Windows CI must supply real SID/image checks."""
    return all(probe(client) is True for probe in (client_sid_check, client_image_check) if probe is not None)


class PipeServer:
    """Own a private socket, event-loop thread and every accepted connection.

    app_factory is called on the server thread and normally returns
    create_app(runtime_engine, model, run_id=run_id). A ListenerApp is also
    accepted so a fake streaming app can supply an explicit test route table.
    check is settable and receives client once per connection.
    """

    def __init__(self, app_factory, *, run_id=None, run_secret=None, check=check_client,
                 client="test-host", directory=None):
        __tracebackhide__ = True
        self.app_factory = app_factory
        self.run_id = run_id if run_id is not None else secrets.token_hex(16)
        self.run_secret = run_secret if run_secret is not None else secrets.token_bytes(32)
        self.check, self.client = check, client
        self._directory = directory if directory is not None else Path.cwd()
        self._ready = threading.Event()
        self._thread = None
        self._temporary = None
        self._failure = None
        self._writers, self._tasks = set(), set()

    def verify(self, challenge, *, server_sid_check=None, server_image_check=None):
        """Fake host-check answer; native identity probes must be supplied on Windows."""
        __tracebackhide__ = True
        if any(probe() is not True for probe in (server_sid_check, server_image_check) if probe is not None):
            return None
        return hmac_check(self.run_secret, self.run_id, challenge)

    def start(self):
        if self._thread is not None:
            raise RuntimeError("Server already started")
        self._temporary = tempfile.TemporaryDirectory(prefix=".host-", dir=self._directory)
        self.folder = Path(self._temporary.name)
        self.folder.chmod(0o700)
        self.path = self.folder / "pipe.sock"
        self._thread = threading.Thread(target=self._thread_main, name="host-test-server", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            self.close()
            raise RuntimeError("Server did not start")
        if self._failure is not None:
            self.close()
            raise RuntimeError("Server startup failed") from self._failure
        return self

    def _thread_main(self):
        try:
            asyncio.run(self._run())
        except BaseException as exc:
            self._failure = exc
        finally:
            self._ready.set()

    async def _run(self):
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        app = self.app_factory()
        self.app = app if isinstance(app, ListenerApp) else ListenerApp(app, "host")

        async def accept(reader, writer):
            task = asyncio.current_task()
            self._writers.add(writer)
            self._tasks.add(task)
            try:
                await serve_connection(reader, writer, self.app, client=self.client, check=self.check)
            finally:
                self._writers.discard(writer)
                self._tasks.discard(task)

        server = await asyncio.start_unix_server(accept, path=str(self.path))
        self._ready.set()
        try:
            await self._stop.wait()
        finally:
            server.close()
            writers, tasks = list(self._writers), list(self._tasks)
            for writer in writers:
                writer.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(*(writer.wait_closed() for writer in writers), return_exceptions=True)
            await server.wait_closed()

    @property
    def connection_count(self):
        async def count():
            return len(self._writers)
        return asyncio.run_coroutine_threadsafe(count(), self._loop).result(timeout=5)

    def close(self):
        if self._thread is None:
            return
        if self._thread.is_alive() and hasattr(self, "_stop"):
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(10)
        if self._thread.is_alive():
            raise RuntimeError("Server did not stop")
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()


@dataclass(init=False, repr=False)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    method: str = "UNKNOWN"
    path: str = "/"

    def __init__(self, status, headers, body, method="UNKNOWN", path="/"):
        __tracebackhide__ = True
        self.status, self.headers, self.body = status, headers, body
        self.method, self.path = method, path

    def json(self):
        __tracebackhide__ = True
        try:
            return jsonlib.loads(self.body)
        except Exception as exc:
            kind = type(exc).__name__
        _fail(self.method, self.path, self.status, kind)

    def require_status(self, status):
        __tracebackhide__ = True
        if self.status != status:
            _fail(self.method, self.path, self.status, "AssertionError")
        return self


@dataclass(frozen=True)
class Event:
    data: str
    event: str = "message"
    id: str | None = None


class HostClient:
    """Verify, mint and exchange once; retain only the session secret in memory.

    mint() must use an owner transaction and bind its code to run_id. verify
    receives a new 256-bit challenge. Neither callback is retained after login.
    """

    def __init__(self, path, *, run_id, run_secret, mint, verify=None):
        __tracebackhide__ = True
        status = None
        try:
            self.path, self.secret = Path(path), None
            challenge = secrets.token_bytes(32)
            try:
                answer = verify(challenge) if verify is not None else None
            except Exception:
                # Preserve the host-check contract's fixed, credential-free refusal.
                raise ServerRefused("Server verification failed") from None
            expected = hmac_check(run_secret, run_id, challenge)
            if not isinstance(answer, bytes) or not hmac.compare_digest(expected, answer):
                raise ServerRefused("Server verification failed") from None
            mint_kind = None
            try:
                code = mint()
            except Exception as exc:
                # Callback errors are untrusted even when they use our error classes.
                mint_kind = type(exc).__name__
            if mint_kind is not None:
                _fail("POST", "/v1/session", status, mint_kind)
            response = self.request("POST", "/v1/session", json={"sign_in_code": code})
            status = response.status
            if status != 200:
                _fail("POST", "/v1/session", status, "AssertionError", ServerRefused)
            self.secret = response.json()["session_secret"]
        except ClientError:
            raise
        except Exception as exc:
            kind = type(exc).__name__
        else:
            return
        _fail("POST", "/v1/session", status, kind)

    def __repr__(self):
        return "<HostClient>"

    __str__ = __repr__

    def with_secret(self, secret):
        """An independent credential variant; it retains no reference to this client."""
        __tracebackhide__ = True
        try:
            variant = object.__new__(type(self))
            variant.path, variant.secret = self.path, secret
            return variant
        except Exception as exc:
            kind = type(exc).__name__
        _fail("POST", "/v1/session", None, kind)

    def without_authorization(self):
        __tracebackhide__ = True
        return self.with_secret(None)

    def _open(self, method, path, *, json=None, headers=None, raw_authorization=_DEFAULT):
        __tracebackhide__ = True
        connection = response = None
        try:
            body = b"" if json is None else jsonlib.dumps(json, separators=(",", ":")).encode("utf-8")
            fields = {"Host": "engine", "Connection": "close", "Content-Length": str(len(body))}
            if json is not None:
                fields["Content-Type"] = "application/json"
            authorization = (f"Session {self.secret}" if self.secret is not None else None
                             ) if raw_authorization is _DEFAULT else raw_authorization
            if authorization is not None:
                fields["Authorization"] = authorization
            # Header names are case insensitive, including explicit hostile overrides.
            for key, value in (headers or {}).items():
                for existing in tuple(fields):
                    if existing.lower() == key.lower():
                        del fields[existing]
                fields[key] = value
            if not method.isalpha() or not path.startswith("/") or any(c in path for c in "\r\n "):
                raise ValueError("Invalid HTTP request")
            wire = f"{method} {path} HTTP/1.1\r\n".encode("ascii")
            for key, value in fields.items():
                if "\r" in key + value or "\n" in key + value:
                    raise ValueError("Invalid HTTP request")
                wire += f"{key}: {value}\r\n".encode("latin-1")
            wire += b"\r\n" + body
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(5)
            connection.connect(str(self.path))
            connection.sendall(wire)
            response = http.client.HTTPResponse(connection, method=method)
            response.begin()
            return connection, response
        except BaseException as exc:
            # Cleanup must not replace the original failure with an unsafe close error.
            _close(connection, response)
            if not isinstance(exc, Exception):
                raise
            kind = type(exc).__name__
            status = None if response is None or not isinstance(response.status, int) else response.status
        _fail(method, path, status, kind)

    def request(self, method, path, json=None, headers=None, raw_authorization=_DEFAULT):
        __tracebackhide__ = True
        status = None
        try:
            connection, response = self._open(method, path, json=json, headers=headers,
                                              raw_authorization=raw_authorization)
            try:
                status = response.status
                fields = {}
                for key, value in response.getheaders():
                    fields[key.lower()] = value
                return Response(status, fields, response.read(), method, path)
            finally:
                kind = _close(connection, response)
                if kind is not None:
                    _fail(method, path, status, kind)
        except ClientError:
            raise
        except Exception as exc:
            kind = type(exc).__name__
        _fail(method, path, status, kind)

    def stream(self, path, last_event_id=None):
        """Lazily yield SSE Events; close the generator (or exhaust it) to release the socket."""
        __tracebackhide__ = True
        status = None
        try:
            headers = {"Accept": "text/event-stream"}
            if last_event_id is not None:
                headers["Last-Event-ID"] = str(last_event_id)
            connection, response = self._open("GET", path, headers=headers)
            try:
                status = response.status
                if status != 200:
                    _fail("GET", path, status, "AssertionError")
                data, event, identity = [], "message", None
                while raw := response.readline():
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if not line:
                        if data:
                            yield Event("\n".join(data), event, identity)
                        data, event = [], "message"
                        continue
                    if line.startswith(":"):
                        continue
                    key, _, value = line.partition(":")
                    value = value.removeprefix(" ")
                    if key == "data":
                        data.append(value)
                    elif key == "event":
                        event = value
                    elif key == "id" and "\0" not in value:
                        identity = value
            finally:
                kind = _close(connection, response)
                if kind is not None:
                    _fail("GET", path, status, kind)
        except ClientError:
            raise
        except Exception as exc:
            kind = type(exc).__name__
        else:
            return
        _fail("GET", path, status, kind)

    def sign_out(self):
        __tracebackhide__ = True
        return self.request("DELETE", "/v1/session")


def books(engine):
    return snapshot(engine)


def session_writes(before, after):
    return {name: diff for name, diff in changes(before, after).items() if name in SESSION_TABLES}


def business_changes(before, after):
    """Keep every collateral table, including audit_log, visible to assertions."""
    return {name: diff for name, diff in changes(before, after).items() if name not in SESSION_TABLES}


def unchanged(before, after):
    assert business_changes(before, after) == {}


@dataclass(repr=False)
class HostWorld:
    owner: object
    runtime: object
    model: ScriptedModel
    companies: tuple[str, str]
    principals: dict
    tokens: dict = field(repr=False)

    def mint(self, name, run_id):
        __tracebackhide__ = True
        with self.owner.begin() as conn:
            return admin.mint_sign_in_code(conn, name, "test", run_id)

    def server(self, **kwargs):
        __tracebackhide__ = True
        run_id = kwargs.pop("run_id", secrets.token_hex(16))
        return PipeServer(lambda: create_app(self.runtime, self.model, run_id=run_id), run_id=run_id, **kwargs)

    def client(self, server, name="alice", **kwargs):
        __tracebackhide__ = True
        return HostClient(server.path, run_id=server.run_id, run_secret=server.run_secret,
                          mint=lambda: self.mint(name, server.run_id), verify=server.verify, **kwargs)


def make_world(owner, runtime, replies=()):
    """Extend the suite's seeded books using the approval tests' principal pattern."""
    with owner.connect() as conn:
        first = company_ctx(conn).company_id
    second = create_company(owner, "Other Co")
    people, tokens = {}, {}
    with owner.begin() as conn:
        for name, cid in (("alice", first), ("bob", second)):
            tokens[name] = admin.add_principal(conn, name, ["view", "post", "approve"], [cid])
            people[name] = pr.authenticate(conn, tokens[name])
    return HostWorld(owner, runtime, ScriptedModel(list(replies)), (first, second), people, tokens)


@pytest.fixture
def host_world(engine, runtime_login):
    runtime = create_engine(runtime_login)
    try:
        yield make_world(engine, runtime)
    finally:
        runtime.dispose()
