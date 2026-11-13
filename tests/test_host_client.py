"""Desktop-host harness contracts through real Unix byte streams and PostgreSQL."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import socket
import stat
import threading
import time

import pytest
from fastapi import Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from bookdiff import assert_changes
from host_client import (
    Event, HostClient, PipeServer, ServerRefused, books, business_changes,
    check_client, hmac_check, host_world, session_writes, unchanged,
)
from erpclaw_api.app import create_app
from erpclaw_api.pipe import LISTENER_TABLES, ListenerApp
from erpclaw_assistant.model import Reply
from erpclaw_core import models as m, principals as pr


def refused(response):
    assert response.status == 401
    assert response.json() == {"detail": "Sign in to use the engine.", "code": "unauthenticated"}


def only_session_audits(before, after, actions):
    """Audit is collateral unless its precise session action is expected."""
    business_before = {k: v for k, v in before.items() if k not in {"principal_session", "principal_sign_in"}}
    business_after = {k: v for k, v in after.items() if k not in {"principal_session", "principal_sign_in"}}
    assert_changes(business_before, business_after, {
        "audit_log": {"added": [{"action": action, "actor": actor} for action, actor in actions]},
    })


def test_host_check_authenticates_label_key_run_and_challenge():
    """Removing the HMAC domain label, key, run binding or challenge binding must fail."""
    key, run, challenge = b"test-run-secret", "run-one", bytes(range(32))
    message = b"erpclaw-host-check-v1\0\0\0\0\x07run-one" + challenge
    expected = hmac.new(key, message, hashlib.sha256).digest()
    assert hmac_check(key, run, challenge) == expected
    assert len(expected) == 32
    assert hmac_check(key, "run-two", challenge) != expected
    assert hmac_check(b"another-key", run, challenge) != expected
    assert hmac_check(key, run, b"another-challenge") != expected
    assert hmac_check(key, "ab", b"c") != hmac_check(key, "a", b"bc")


@pytest.mark.parametrize("answer", ["wrong", "missing", "missing_callable", "another_run", "exception"])
def test_unverified_server_never_mints_sends_or_creates_session(host_world, answer):
    """Server verification must refuse before minting a code or opening a credential connection."""
    world = host_world
    minted, accepted = [], []
    with world.server(check=lambda peer: accepted.append(peer) or True) as server:
        before = books(world.owner)

        def mint():
            minted.append(True)
            return world.mint("alice", server.run_id)

        def verify(challenge):
            if answer == "wrong":
                return b"x" * 32
            if answer == "missing":
                return None
            if answer == "another_run":
                return hmac_check(server.run_secret, "previous-run", challenge)
            raise OSError("verification unavailable")

        with pytest.raises(ServerRefused, match="Server verification failed"):
            HostClient(server.path, run_id=server.run_id, run_secret=server.run_secret, mint=mint,
                       verify=None if answer == "missing_callable" else verify)
        after = books(world.owner)
        assert minted == accepted == []
        assert after["principal_session"] == after["principal_sign_in"] == {}
        assert session_writes(before, after) == {}
        unchanged(before, after)


def test_good_server_signs_in_with_bound_code_and_constant_time_check(host_world, monkeypatch):
    """Verification uses fresh challenges and compare_digest; login spends a run-bound code once."""
    world = host_world
    comparisons, challenges = [], []
    original_compare = hmac.compare_digest

    def compare(left, right):
        comparisons.append((len(left), len(right)))
        return original_compare(left, right)

    monkeypatch.setattr(hmac, "compare_digest", compare)
    with world.server() as server:
        before = books(world.owner)

        def verify(challenge):
            challenges.append(challenge)
            return server.verify(challenge)

        clients = [HostClient(server.path, run_id=server.run_id, run_secret=server.run_secret,
                              mint=lambda: world.mint("alice", server.run_id), verify=verify) for _ in range(2)]
        assert len(comparisons) == 2 and comparisons == [(32, 32), (32, 32)]
        assert len(set(challenges)) == 2 and all(len(c) == 32 for c in challenges)
        for client in clients:
            response = client.request("POST", "/v1/conversations", json={})
            assert response.status == 200 and set(response.json()) == {"id"}
        after = books(world.owner)
        diff = session_writes(before, after)
        assert set(diff) == {"principal_session", "principal_sign_in"}
        assert len(diff["principal_session"]["added"]) == len(diff["principal_sign_in"]["added"]) == 2
        for table in diff.values():
            assert table["changed"] == table["removed"] == []
        assert {row["secret_sha256"] for row in diff["principal_session"]["added"]} == {
            pr.session_digest(client.secret) for client in clients}
        for row in diff["principal_sign_in"]["added"]:
            assert row["run_id"] == server.run_id and row["used_at"] is not None
            assert row["principal_id"] == world.principals["alice"]["id"]
        only_session_audits(before, after, [("mint_sign_in_code", "test"), ("sign_in", "alice")] * 2)


def test_credentials_stay_in_memory_and_out_of_files_environment_logs_and_output(host_world, tmp_path,
                                                                               caplog, capfd):
    """Writing or printing a code/session secret, or retaining the code after exchange, must fail."""
    world = host_world
    minted = []
    caplog.set_level(logging.DEBUG, logger="erpclaw_api")
    caplog.set_level(logging.DEBUG, logger="erpclaw_core")
    with world.server() as server:
        def mint():
            code = world.mint("alice", server.run_id)
            minted.append(code)  # Test-only witness, never retained by the harness.
            return code

        client = HostClient(server.path, run_id=server.run_id, run_secret=server.run_secret,
                            mint=mint, verify=server.verify)
        assert client.request("POST", "/v1/conversations").status == 200
        assert client.sign_out().status == 204
        captured = capfd.readouterr()
        visible = [captured.out, captured.err, caplog.text, repr(os.environ), repr(client)]
        visible.extend(record.getMessage() + repr(record.__dict__) for record in caplog.records)
        for folder in (tmp_path, server.folder):
            for path in folder.rglob("*"):
                visible.append(str(path))
                if stat.S_ISREG(path.stat().st_mode):
                    visible.append(path.read_bytes().decode("utf-8", errors="replace"))
        for credential in (client.secret, minted[0]):
            assert all(credential not in output for output in visible)
        assert vars(client) == {"path": server.path, "secret": client.secret}
        assert minted[0] not in repr(vars(client))


def test_requests_send_session_header_and_no_browser_credentials(host_world):
    """Removing Authorization or adding browser Cookie/Origin/Host/custom headers must fail."""
    world, observed = host_world, []

    def factory():
        app = create_app(world.runtime, world.model, run_id="wire-run")

        @app.middleware("http")
        async def observe(request: Request, call_next):
            observed.append((request.method, request.url.path, dict(request.headers)))
            response = await call_next(request)
            response.set_cookie("ignored", "browser-cookie")
            return response

        return app

    with PipeServer(factory, run_id="wire-run") as server:
        client = world.client(server)
        assert client.request("GET", "/v1/health").json() == {"ok": True}
        assert client.request("POST", "/v1/conversations", json={}, headers={"X-Test": "yes"}).status == 200
        assert client.sign_out().status == 204
        assert [(method, path) for method, path, _ in observed] == [
            ("POST", "/v1/session"), ("GET", "/v1/health"), ("POST", "/v1/conversations"),
            ("DELETE", "/v1/session")]
        for index, (_, _, headers) in enumerate(observed):
            assert headers["host"] == "engine"  # Required synthetic authority in the byte-stream parser.
            assert not {"cookie", "origin", "x-erpclaw-request"} & headers.keys()
            if index == 0:
                assert "authorization" not in headers
            else:
                assert headers["authorization"] == f"Session {client.secret}"


@pytest.mark.parametrize("variant", ["missing", "unknown", "raw_missing", "raw_unknown"])
def test_bad_or_absent_secret_is_unauthenticated_without_writes(host_world, variant):
    """Protected routes must reject absent/never-issued secrets before any business or session write."""
    world = host_world
    with world.server() as server:
        client = world.client(server)
        hostile = client.without_authorization() if variant == "missing" else client.with_secret("never-issued")
        kwargs = {}
        if variant.startswith("raw_"):
            hostile = client
            kwargs["raw_authorization"] = None if variant == "raw_missing" else "Session never-issued"
        before = books(world.owner)
        for method, path, body in [
            ("POST", "/v1/conversations", {}),
            ("POST", "/v1/conversations/unknown/messages", {"text": "hello"}),
            ("POST", "/v1/cards/unknown/approve", {"digest": "0" * 64}),
            ("POST", "/v1/cards/unknown/decline", {}),
            ("GET", "/v1/operations/unknown", None), ("GET", "/v1/me", None),
        ]:
            refused(hostile.request(method, path, json=body, **kwargs))
        after = books(world.owner)
        unchanged(before, after)
        assert session_writes(before, after) == {}
        assert client.secret is not None
        if not variant.startswith("raw_"):
            assert vars(hostile) == {"path": client.path, "secret": None if variant == "missing" else "never-issued"}


def test_sign_out_revokes_secret_and_repeated_or_anonymous_sign_out_is_noop(host_world):
    """Sign-out must end the session; reused secrets fail and idempotent DELETE writes nothing."""
    world = host_world
    with world.server() as server:
        client = world.client(server)
        before = books(world.owner)
        response = client.sign_out()
        assert response.status == 204 and response.body == b""
        after = books(world.owner)
        diff = session_writes(before, after)
        assert set(diff) == {"principal_session"}
        assert diff["principal_session"]["added"] == diff["principal_session"]["removed"] == []
        [(old, ended)] = diff["principal_session"]["changed"]
        assert old["ended_at"] is None and ended["ended_at"] is not None
        assert ended["ended_why"] == "sign_out"
        assert {k for k in old if old[k] != ended[k]} == {"ended_at", "ended_why"}
        only_session_audits(before, after, [("sign_out", "alice")])
        refused(client.request("POST", "/v1/conversations"))
        for variant in (client, client.without_authorization(), client.with_secret("never-issued")):
            response = variant.sign_out()
            assert response.status == 204 and response.body == b""
        final = books(world.owner)
        unchanged(after, final)
        assert session_writes(after, final) == {}


def test_two_principals_have_separate_grants_tokens_and_sessions(host_world):
    """Clients must have distinct secrets bound to their own principal and company grants."""
    world = host_world
    assert len(set(world.companies)) == 2
    with world.owner.connect() as conn:
        assert len(conn.execute(select(m.company.c.id)).scalars().all()) == 2
        for name, company in zip(("alice", "bob"), world.companies):
            who = pr.authenticate(conn, world.tokens[name])
            assert who["id"] == world.principals[name]["id"]
            grants = conn.execute(select(m.principal_grant).where(
                m.principal_grant.c.principal_id == who["id"])).mappings().all()
            assert {(row["company_id"], row["permission"]) for row in grants} == {
                (company, permission) for permission in ("view", "post", "approve")}
    with world.server() as server:
        alice, bob = world.client(server), world.client(server, "bob")
        assert alice.secret != bob.secret
        snapshot = books(world.owner)
        for name, client in (("alice", alice), ("bob", bob)):
            row, = [row for row in snapshot["principal_session"].values()
                    if row["secret_sha256"] == pr.session_digest(client.secret)]
            assert row["principal_id"] == world.principals[name]["id"]
        assert alice.sign_out().status == 204
        refused(alice.request("POST", "/v1/conversations"))
        assert bob.request("POST", "/v1/conversations").status == 200
        world.model.replies.append(Reply("A scripted answer."))
        assert world.model.complete([], []).text == "A scripted answer."


def test_stream_is_lazy_authorized_resumable_and_closes_on_early_exit(host_world):
    """Streams must carry Session/Last-Event-ID, parse incremental SSE and close on generator.close()."""
    world, observed = host_world, []
    disconnected = threading.Event()

    def factory():
        app = create_app(world.runtime, world.model, run_id="stream-run")

        @app.get("/v1/test-events")
        async def stream(request: Request):
            observed.append(dict(request.headers))

            async def events():
                try:
                    yield b": keepalive\r\nid: 8\r\nevent: answer\r\ndata: first\r\n"
                    yield b"data: line\r\n\r\ndata: second\n\n"
                    # No third event: advancing would block, proving incremental consumption.
                    while not await request.is_disconnected():
                        await asyncio.sleep(0.005)
                finally:
                    disconnected.set()

            return StreamingResponse(events(), media_type="text/event-stream")

        # A fake stream is explicit: the production table has no stream route yet.
        return ListenerApp(app, "host", table=LISTENER_TABLES["host"] | {("GET", "/v1/test-events")})

    with PipeServer(factory, run_id="stream-run") as server:
        client = world.client(server)
        events = client.stream("/v1/test-events", last_event_id="7")
        assert observed == []
        try:
            assert next(events) == Event("first\nline", "answer", "8")
            assert next(events) == Event("second", "message", "8")
            headers, = observed
            assert headers["authorization"] == f"Session {client.secret}"
            assert headers["last-event-id"] == "7" and headers["accept"] == "text/event-stream"
            assert headers["host"] == "engine" and not {"cookie", "origin", "x-erpclaw-request"} & headers.keys()
            assert server.connection_count == 1
        finally:
            events.close()
        assert disconnected.wait(2), "Closing the generator did not disconnect the server stream"
        deadline = time.monotonic() + 2
        while server.connection_count and time.monotonic() < deadline:
            time.sleep(0.005)
        assert server.connection_count == 0


def test_server_checks_connections_and_close_removes_socket_and_thread(host_world):
    """The acceptor must enforce settable peer checks and close idle connections/remove its private socket."""
    world, checked = host_world, []
    with world.server() as server:
        path, folder, thread = server.path, server.folder, server._thread
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700 and stat.S_ISSOCK(path.stat().st_mode)
        client = world.client(server)
        server.check = lambda peer: checked.append(peer) or False
        with pytest.raises((ConnectionError, OSError)):
            client.request("GET", "/v1/health")
        assert checked == [server.client]
        idle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        idle.settimeout(2)
        idle.connect(str(path))
        idle.sendall(b"GET /v1/health")  # An incomplete first line remains owned by the server.
        deadline = time.monotonic() + 2
        while not server.connection_count and time.monotonic() < deadline:
            time.sleep(0.005)
        assert server.connection_count == 1
    try:
        assert idle.recv(1) == b""
    finally:
        idle.close()
    assert not path.exists() and not folder.exists() and not thread.is_alive()
    server.close()  # Cleanup is safe to repeat.


def test_identity_hooks_refuse_when_supplied_checks_fail(host_world):
    """Supplied SID/image callbacks must fail closed; these fake probes do not test Windows identity."""
    assert check_client("peer", client_sid_check=lambda peer: peer == "peer",
                        client_image_check=lambda peer: True)
    assert not check_client("peer", client_sid_check=lambda peer: False)
    assert not check_client("peer", client_image_check=lambda peer: False)
    with host_world.server() as server:
        assert server.verify(b"challenge", server_sid_check=lambda: False) is None
        assert server.verify(b"challenge", server_image_check=lambda: False) is None
        assert server.verify(b"challenge", server_sid_check=lambda: True,
                             server_image_check=lambda: True) == hmac_check(
                                 server.run_secret, server.run_id, b"challenge")


def test_book_diff_separates_only_session_tables():
    """Session exemptions must preserve audit and every business table as collateral."""
    before = {name: {} for name in ("principal_session", "principal_sign_in", "audit_log", "invoice")}
    after = {name: {(1,): {"id": 1}} for name in before}
    assert set(session_writes(before, after)) == {"principal_session", "principal_sign_in"}
    assert set(business_changes(before, after)) == {"audit_log", "invoice"}
    with pytest.raises(AssertionError):
        unchanged(before, after)
