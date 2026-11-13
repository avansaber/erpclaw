"""Credential boundaries stay private in rendered failures and real pytest reports."""
from __future__ import annotations

import http.client
import json
import socket
import struct
import tempfile
import threading
from pathlib import Path

import pytest

import test_api_approval_security as approval_tests
from host_client import ClientError, HostClient, Response, ServerRefused, hmac_check, host_world
from test_api_approval_security import approval_world, signed


def credentials():
    # Assemble witnesses so traceback source lines cannot contain their values.
    return tuple("private-witness-" + kind + "-credential" for kind in ("code", "session", "run"))


def exercise(case):
    __tracebackhide__ = True
    code, secret, run_secret = credentials()
    run_id = "redaction-run"
    authenticated = case.startswith("authenticated_")
    fault = case.removeprefix("authenticated_").removeprefix("bootstrap_")
    with tempfile.TemporaryDirectory(prefix=".redaction-", dir=Path(__file__).resolve().parents[1]) as folder:
        path = Path(folder) / "pipe.sock"
        failures = []

        def respond():
            __tracebackhide__ = True
            try:
                exchanges = 2 if authenticated and fault != "missing" else 1
                for index in range(exchanges):
                    with listener.accept()[0] as peer:
                        peer.settimeout(5)
                        header = b""
                        # Leave the request body unread for the reset case.
                        while not header.endswith(b"\r\n\r\n"):
                            part = peer.recv(1)
                            if not part:
                                raise RuntimeError("Request ended before its headers")
                            header += part
                        good_login = authenticated and index == 0
                        if fault != "reset" or good_login:
                            length = int(next(line.split(b":", 1)[1] for line in header.split(b"\r\n")
                                              if line.lower().startswith(b"content-length:")))
                            while length:
                                part = peer.recv(length)
                                if not part:
                                    raise RuntimeError("Request ended before its body")
                                length -= len(part)
                        body = json.dumps({"session_secret": secret, "sign_in_code": code}).encode()
                        status = b"200 OK"
                        if not good_login:
                            if fault == "json":
                                body = body[:-1]
                            elif fault == "status":
                                status = b"503 " + code.encode() + b" " + secret.encode()
                            elif fault == "reset":
                                peer.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 10000\r\n\r\n" + body)
                                peer.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                                continue
                        peer.sendall(b"HTTP/1.1 " + status + b"\r\nContent-Length: " + str(len(body)).encode()
                                     + b"\r\nX-Witness: " + secret.encode() + b"\r\n\r\n" + body)
            except Exception as exc:
                failures.append(type(exc).__name__)

        def login():
            __tracebackhide__ = True
            return HostClient(path, run_id=run_id, run_secret=run_secret.encode(), mint=lambda: code,
                              verify=lambda challenge: hmac_check(run_secret.encode(), run_id, challenge))

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.settimeout(5)
            if fault != "missing" or authenticated:
                listener.bind(str(path))
                listener.listen()
                worker = threading.Thread(target=respond, daemon=True)
                worker.start()
            try:
                client = login()
                if authenticated and fault == "missing":
                    worker.join(6)
                    listener.close()
                    path.unlink()
                if authenticated:
                    if fault == "status":
                        # An untrusted stream cursor may itself contain credential text.
                        next(client.stream("/v1/events", last_event_id=secret))
                    else:
                        client.request("POST", "/v1/conversations", json={"sign_in_code": code}).json()
            finally:
                if fault != "missing" or case.startswith("authenticated_"):
                    worker.join(6)
                    if worker.is_alive() or failures:
                        raise AssertionError("The response server did not finish cleanly") from None


@pytest.mark.parametrize("case", [
    "bootstrap_missing", "authenticated_missing", "bootstrap_reset", "authenticated_reset",
    "bootstrap_json", "authenticated_json", "bootstrap_status", "authenticated_status",
])
def test_failures_omit_credentials(case, request, capsys):
    __tracebackhide__ = True
    with pytest.raises(Exception) as failure:
        exercise(case)
    reports = {"exception": str(failure.value)}
    for style in ("long", "short", "native"):
        reports[style] = str(failure.getrepr(style=style, showlocals=True, funcargs=True))

    class ClientFailure:
        @pytest.fixture
        def client_failure(self):
            return lambda: exercise(case)

    pytester = request.getfixturevalue("pytester")
    pytester.makepyfile("def test_client_failure(client_failure):\n    client_failure()\n")
    try:
        result = pytester.runpytest_inprocess("-q", "--tb=long", "--showlocals", plugins=[ClientFailure()])
    finally:
        captured = capsys.readouterr()
    reports.update({"pytest stdout": result.stdout.str(), "pytest stderr": result.stderr.str(),
                    "captured stdout": captured.out, "captured stderr": captured.err})
    leaked = [label for label, report in reports.items() if any(value in report for value in credentials())]
    if leaked:
        raise AssertionError("Credential exposed in: " + ", ".join(leaked)) from None
    result.assert_outcomes(failed=1)
    method, path = ("POST", "/v1/conversations") if case.startswith("authenticated_") else ("POST", "/v1/session")
    fault = case.rsplit("_", 1)[1]
    kind = {"missing": "FileNotFoundError", "reset": "ConnectionResetError", "json": "JSONDecodeError",
            "status": "AssertionError"}[fault]
    status_text = "" if fault == "missing" else f": HTTP {503 if fault == 'status' else 200}"
    if case == "authenticated_status":
        method, path = "GET", "/v1/events"
    expected = f"{method} {path}{status_text} ({kind})"
    assert str(failure.value) == expected
    assert type(failure.value).__name__ == ("ServerRefused" if case == "bootstrap_status" else "ClientError")
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None
    assert failure.value.__suppress_context__
    assert expected in result.stdout.str()


def test_representations_omit_credentials():
    __tracebackhide__ = True
    code, secret, run_secret = credentials()
    client = object.__new__(HostClient)
    client.path, client.secret = Path(code), secret
    response = Response(200, {"Authorization": secret}, json.dumps({"sign_in_code": code}).encode())
    values = (client, client.with_secret(run_secret), client.without_authorization(), response)
    if any(credential in visible for value in values for visible in (str(value), repr(value))
           for credential in credentials()):
        raise AssertionError("A representation exposed a credential") from None


def test_response_status_check_omits_body_and_headers():
    __tracebackhide__ = True
    code, secret, _ = credentials()
    response = Response(503, {"Authorization": secret}, json.dumps({"sign_in_code": code}).encode(),
                        "POST", "/v1/conversations")
    with pytest.raises(OSError) as failure:
        response.require_status(200)
    for style in ("long", "short", "native"):
        report = str(failure.getrepr(style=style, showlocals=True, funcargs=True))
        if any(credential in report for credential in credentials()):
            raise AssertionError("A status check exposed a credential") from None
    assert str(failure.value) == "POST /v1/conversations: HTTP 503 (AssertionError)"
    assert response.require_status(503) is response


@pytest.mark.parametrize("operation", ["request", "stream", "mint"])
@pytest.mark.parametrize("error_type", [OSError, socket.timeout, http.client.BadStatusLine, AssertionError])
def test_exception_messages_and_chains_are_replaced(operation, error_type, monkeypatch):
    __tracebackhide__ = True
    code, secret, run_secret = credentials()

    def fail(*args, **kwargs):
        __tracebackhide__ = True
        try:
            raise ValueError(code)
        except ValueError as cause:
            raise error_type(secret) from cause

    with pytest.raises(Exception) as failure:
        if operation == "mint":
            HostClient("unused.sock", run_id="redaction-run", run_secret=run_secret.encode(), mint=fail,
                       verify=lambda challenge: hmac_check(run_secret.encode(), "redaction-run", challenge))
        else:
            monkeypatch.setattr(HostClient, "_open", fail)
            client = object.__new__(HostClient)
            client.path, client.secret = Path("unused.sock"), secret
            if operation == "request":
                client.request("POST", "/v1/conversations", json={"sign_in_code": code})
            else:
                next(client.stream("/v1/events"))
    for style in ("long", "short", "native"):
        report = str(failure.getrepr(style=style, showlocals=True, funcargs=True))
        if any(credential in report for credential in credentials()):
            raise AssertionError("An exception exposed a credential") from None
    method = "GET" if operation == "stream" else "POST"
    path = {"mint": "/v1/session", "request": "/v1/conversations", "stream": "/v1/events"}[operation]
    assert str(failure.value) == f"{method} {path} ({error_type.__name__})"
    assert type(failure.value).__name__ == "ClientError"
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None
    assert failure.value.__suppress_context__


def private_failure_reports(failure, witnesses, request, capsys):
    """Inspect rendered failures and an actual pytest report without publishing a leak."""
    __tracebackhide__ = True
    reports = {"exception": str(failure.value)}
    for style in ("long", "short", "native"):
        reports[style] = str(failure.getrepr(style=style, showlocals=True, funcargs=True))

    class SavedFailure:
        @pytest.fixture
        def saved_failure(self):
            __tracebackhide__ = True

            def fail():
                __tracebackhide__ = True
                raise failure.value.with_traceback(failure.value.__traceback__)

            return fail

    # Defer pytester's cwd change until the caller has closed its Unix socket.
    pytester = request.getfixturevalue("pytester")
    pytester.makepyfile("def test_saved_failure(saved_failure):\n    saved_failure()\n")
    try:
        result = pytester.runpytest_inprocess("-q", "--tb=long", "--showlocals", plugins=[SavedFailure()])
    finally:
        captured = capsys.readouterr()
    reports.update({"pytest stdout": result.stdout.str(), "pytest stderr": result.stderr.str(),
                    "captured stdout": captured.out, "captured stderr": captured.err})
    leaked = [label for label, report in reports.items() if any(value in report for value in witnesses)]
    if leaked:
        raise AssertionError("Credential exposed in: " + ", ".join(leaked)) from None
    result.assert_outcomes(failed=1)
    return reports


def test_approval_caller_conversation_failure_omits_credentials(signed, monkeypatch, request, capsys):
    __tracebackhide__ = True
    host_request = HostClient.request
    witnesses = []

    def unexpected_status(self, method, path, **kwargs):
        __tracebackhide__ = True
        if method == "POST" and path == "/v1/conversations":
            return Response(503, {}, b"{}", method, path)
        response = host_request(self, method, path, **kwargs)
        if method == "POST" and path == "/v1/session":
            witnesses.append(kwargs["json"]["sign_in_code"])
            if response.status == 200:
                witnesses.append(response.json()["session_secret"])
        return response

    monkeypatch.setattr(HostClient, "request", unexpected_status)
    with pytest.raises(AssertionError) as failure:
        approval_tests.test_sign_in_code_is_run_bound_and_spent_once(signed)
    # Foreign code, exchanged code, session secret, replayed code.
    assert len(witnesses) == 4 and len(set(witnesses)) == 3
    reports = private_failure_reports(failure, witnesses, request, capsys)
    for style in ("long", "short", "pytest stdout"):
        report = reports[style]
        assert "test_sign_in_code_is_run_bound_and_spent_once" in report
        assert "POST" in report and "/v1/conversations" in report and "503" in report


@pytest.mark.parametrize("error_type", [ClientError, ServerRefused])
def test_mint_client_errors_and_chains_are_replaced(error_type, request, capsys):
    __tracebackhide__ = True
    code, secret, run_secret = credentials()

    def fail():
        __tracebackhide__ = True
        try:
            raise ValueError(code)
        except ValueError as cause:
            raise error_type(secret) from cause

    with pytest.raises(ClientError) as failure:
        HostClient("unused.sock", run_id="redaction-run", run_secret=run_secret.encode(), mint=fail,
                   verify=lambda challenge: hmac_check(run_secret.encode(), "redaction-run", challenge))
    reports = private_failure_reports(failure, (code, secret), request, capsys)
    expected = f"POST /v1/session ({error_type.__name__})"
    assert str(failure.value) == expected
    assert type(failure.value) is ClientError
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None
    assert failure.value.__suppress_context__
    assert expected in reports["pytest stdout"]
