"""The invoice approval path through the desktop host's real byte stream."""
from __future__ import annotations

import json
import secrets
from dataclasses import replace
from decimal import Decimal

import pytest
from fastapi import Request
from sqlalchemy import create_engine

from bookdiff import assert_changes
from host_client import ClientError, Concealed, HostClient, HostWorld, PipeServer, books, business_changes, session_writes
from erpclaw_api.app import create_app
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import administration as admin, principals as pr
from erpclaw_core.common import company_ctx
from erpclaw_core.masters import create_item


@pytest.fixture
def walking_world(engine, runtime_login):
    __tracebackhide__ = True
    # One company, as in the message approval tests; no company selection is needed.
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        create_item(ctx, "Consulting", Decimal("150.00"), unit="hour")
        token = Concealed(admin.add_principal(conn, "alice", ["view", "draft", "post", "approve"], [ctx.company_id]))
        alice = pr.authenticate(conn, token)
    runtime = create_engine(runtime_login)
    # The engine calls the model once for the request and again after the owner's confirmation.
    model = ScriptedModel([Reply("", [call(
        "create_invoice", customer="Acme Corporation", items=[{"item": "Consulting", "quantity": 2}],
    )]) for _ in range(2)])
    try:
        yield HostWorld(engine, runtime, model, (ctx.company_id,), {"alice": alice}, {"alice": token})
    finally:
        runtime.dispose()


def test_a_invoice_walking_skeleton(walking_world):
    __tracebackhide__ = True
    world = walking_world
    company = world.companies[0]
    alice = world.principals["alice"]
    run_id = secrets.token_hex(16)
    steps, observed, responses, minted, after_mint = [], [], [], [], []

    def check(condition):
        __tracebackhide__ = True
        # Explicit raises prevent pytest from expanding credential-bearing operands.
        if not condition:
            method, path, status = "POST", "/v1/session", "pending"
            if responses:
                method, path, response = responses[-1]
                status = response.status
            raise AssertionError(f"{method} {path}: HTTP {status}") from None

    def check_omitted(credentials, values):
        __tracebackhide__ = True
        for credential in credentials:
            for value in values:
                check(credential not in value)

    def check_changes(before, after, expected):
        __tracebackhide__ = True
        try:
            assert_changes(before, after, expected)
        except AssertionError:
            # Book-diff diagnostics can include credential or digest-bearing rows.
            check(False)

    def assert_books(before, after, expected=None):
        """Keep every collateral table visible and require session stability after login."""
        __tracebackhide__ = True
        check(session_writes(before, after) == {})
        if expected is None:
            check(business_changes(before, after) == {})
        else:
            check_changes(
                {k: v for k, v in before.items() if k not in {"principal_session", "principal_sign_in"}},
                {k: v for k, v in after.items() if k not in {"principal_session", "principal_sign_in"}},
                expected,
            )

    def factory():
        __tracebackhide__ = True
        app = create_app(world.runtime, world.model, run_id=run_id)

        @app.middleware("http")
        async def observe(request: Request, call_next):
            __tracebackhide__ = True
            # Observe what arrived at the server, without retaining a raw secret.
            credentials = []
            for value in request.headers.getlist("authorization"):
                scheme, _, secret = value.partition(" ")
                credentials.append((scheme, pr.session_digest(secret)))
            observed.append((request.method, request.url.path, credentials))
            return await call_next(request)

        return app

    class ObservedHostClient(HostClient):
        def request(self, method, path, **kwargs):
            __tracebackhide__ = True
            response = super().request(method, path, **kwargs)
            responses.append((method, path, response))
            check_omitted(minted, [
                response.body.decode("utf-8"), *response.headers.keys(), *response.headers.values(),
            ])
            expected = [] if self.secret is None else [("Session", pr.session_digest(self.secret))]
            check(observed[-1] == (method, path, expected))
            if self.secret is not None:
                check_omitted([self.secret], [
                    response.body.decode("utf-8"), *response.headers.keys(), *response.headers.values(),
                ])
            response.require_status(200)
            return response

    with PipeServer(factory, run_id=run_id, check=lambda peer: steps.append("connection checked") or True) as server:
        before_login = books(world.owner)

        def verify(challenge):
            __tracebackhide__ = True
            check(steps == [])
            answer = server.verify(challenge)
            steps.append("server verified")
            return answer

        def mint():
            __tracebackhide__ = True
            check(steps == ["server verified"])
            code = Concealed(world.mint("alice", server.run_id))
            minted.append(code)
            steps.append("code minted")
            after_mint.append(books(world.owner))
            return code

        client = ObservedHostClient(server.path, run_id=server.run_id, run_secret=server.run_secret,
                                    mint=mint, verify=verify)
        check(steps == ["server verified", "code minted", "connection checked"])
        check(observed == [("POST", "/v1/session", [])])
        _, _, exchange = responses[0]
        # Bootstrap returns the new credential once, before authenticated calls can begin.
        check(exchange.json() == {"name": "alice", "session_secret": client.secret})
        check_omitted([client.secret], [
            *exchange.headers.keys(), *exchange.headers.values(),
            json.dumps({k: v for k, v in exchange.json().items() if k != "session_secret"}),
        ])
        signed_in = books(world.owner)
        issued, = after_mint
        code_row, = session_writes(before_login, issued)["principal_sign_in"]["added"]
        session_row, = session_writes(issued, signed_in)["principal_session"]["added"]
        used = signed_in["principal_sign_in"][(pr.code_digest(minted[0]),)]
        check(code_row["code_sha256"] == pr.code_digest(minted[0]))
        check(code_row["principal_id"] == alice["id"] and code_row["run_id"] == server.run_id)
        check(code_row["used_at"] is None and used["used_at"] is not None)
        check(session_row["secret_sha256"] == pr.session_digest(client.secret))
        check(session_row["principal_id"] == alice["id"])
        check(session_row["ended_at"] is None and session_row["ended_why"] is None)
        check(session_row["created_at"] == session_row["last_seen_at"])
        check(session_row["last_seen_at"] < session_row["idle_expires_at"] <= session_row["expires_at"])
        check_changes(before_login, issued, {
            "principal_sign_in": {"added": [code_row]},
            "audit_log": {"added": [{"action": "mint_sign_in_code", "actor": "test"}]},
        })
        check_changes(issued, signed_in, {
            "principal_sign_in": {"changed": [({"code_sha256": code_row["code_sha256"]},
                                                 {"used_at": used["used_at"]})]},
            "principal_session": {"added": [session_row]},
            "audit_log": {"added": [{"action": "sign_in", "actor": "alice"}]},
        })

        conversation = client.request("POST", "/v1/conversations", json={}).json()
        check(set(conversation) == {"id"} and isinstance(conversation["id"], str) and conversation["id"])
        cid = conversation["id"]
        assert_books(signed_in, books(world.owner))
        message_path = f"/v1/conversations/{cid}/messages"
        events = client.request("POST", message_path,
                                json={"text": "invoice Acme for 2 hours of consulting"}).json()["events"]
        check(len(events) == 1 and events[0].get("type") == "ask")
        question, = events
        check(question["code"] == "customer_ambiguous")
        check(question["text"] == "Do you mean Acme Corporation?")
        check(question["options"] == ["Acme Corporation", "someone else"])
        check(question["data"]["confirm"] is True)
        questioned = books(world.owner)
        check(questioned["approval"] == signed_in["approval"])
        assert_books(signed_in, questioned)
        check(len(world.model.seen) == 1 and len(world.model.replies) == 1)

        events = client.request("POST", message_path, json={"text": "yes"}).json()["events"]
        check(len(events) == 1 and events[0].get("type") == "card")
        card, = events
        check(isinstance(card.get("approval_id"), str) and card["approval_id"])
        check(isinstance(card.get("digest"), str) and len(card["digest"]) == 64)
        check(all(c in "0123456789abcdef" for c in card["digest"]))
        check(world.model.seen and not world.model.replies)
        check(any(m.get("content") == "invoice Acme for 2 hours of consulting"
                  for turn in world.model.seen for m in turn))
        check(len(world.model.seen) == 2 and world.model.seen[1][-1] == {"role": "user", "content": "yes"})
        # Hand calculation: 2 hours x 150.00 USD/hour = 300.00 USD; no tax or stock movement.
        lines = dict(card["card"]["lines"])
        check(lines["Item"] == "2 x Consulting @ 150.00 = 300.00")
        check(lines["Total"] == "300.00 USD")
        check("No tax calculated" in lines["Tax"])
        pending = books(world.owner)
        assert_books(signed_in, pending, {
            "approval": {"added": [{"id": card["approval_id"], "digest": card["digest"],
                                      "contract": "create_invoice", "status": "pending", "conversation_id": cid,
                                      "company_id": company, "principal_id": alice["id"]}]},
        })

        approve_path = f"/v1/cards/{card['approval_id']}/approve"
        approved = client.request("POST", approve_path, json={"digest": card["digest"]})
        result_events = approved.json()["events"]
        check(len(result_events) == 1 and result_events[0].get("type") == "result")
        result, = result_events
        check(result["status"] == "committed")
        key = result["operation_key"]
        check(isinstance(key, str) and key)
        check(key == pending["approval"][(card["approval_id"],)]["operation_key"])
        committed = books(world.owner)
        outcome = committed["operation_outcome"][(key,)]
        # The response's operation must be bound to the very card read from the message.
        check({"approval_id": outcome["approval_id"], "digest": outcome["card_digest"]} == {
            "approval_id": card["approval_id"], "digest": card["digest"],
        })
        stored = json.loads(outcome["result_json"])
        check(stored["text"] == result["text"] and stored["amounts"] == result["amounts"])
        check(stored["data"]["total"] == "300.00")
        invoice, = [r for k, r in committed["sales_invoice"].items() if k not in pending["sales_invoice"]]
        posting, = [r for k, r in committed["posting"].items() if k not in pending["posting"]]
        customer, = [r for r in pending["party"].values() if r["company_id"] == company
                     and r["name"] == "Acme Corporation"]
        item, = [r for r in pending["item"].values() if r["company_id"] == company and r["name"] == "Consulting"]
        check(item["standard_rate"] == "150.00" and item["unit"] == "hour" and item["kind"] == "service")
        accounts = {r["account_type"]: r["id"] for r in pending["account"].values() if r["company_id"] == company}
        series = pending["number_series"][(company, "INV", 2026)]
        seq = pending["posting_chain_head"][(company,)]["last_seq"] + 1
        decided = committed["approval"][(card["approval_id"],)]
        check(decided["decided_at"] is not None)
        assert_books(pending, committed, {
            "sales_invoice": {"added": [{"id": invoice["id"], "company_id": company,
                "number": f"INV-2026-{series['next_value']:05d}", "customer_id": customer["id"],
                "posting_date": "2026-10-26", "due_date": "2026-11-25", "currency": "USD",
                "total": "300.00", "outstanding": "300.00", "status": "submitted", "operation_key": key,
                "submit_operation_key": None, "cancel_operation_key": None}]},
            "sales_invoice_line": {"added": [{"company_id": company, "invoice_id": invoice["id"],
                "item_id": item["id"], "currency": "USD", "quantity": "2", "rate": "150.00", "amount": "300.00",
                "line_no": 1, "named_warehouse_id": None, "item_kind": "service"}]},
            "posting": {"added": [{"id": posting["id"], "company_id": company, "ledger": "general",
                "voucher_type": "sales_invoice", "voucher_id": invoice["id"], "seq": seq,
                "posting_date": "2026-10-26"}]},
            "ledger_entry": {"added": [
                {"company_id": company, "posting_id": posting["id"], "account_id": accounts["receivable"],
                 "party_id": customer["id"], "currency": "USD", "debit": "300.00", "credit": "0.00"},
                {"company_id": company, "posting_id": posting["id"], "account_id": accounts["income"],
                 "party_id": None, "currency": "USD", "debit": "0.00", "credit": "300.00"},
            ]},
            "posting_chain_head": {"changed": [({"company_id": company},
                                                 {"last_seq": seq, "last_hash": posting["hash"]})]},
            "number_series": {"changed": [({"company_id": company, "series": "INV", "fiscal_year": 2026},
                                            {"next_value": series["next_value"] + 1})]},
            "approval": {"changed": [({"id": card["approval_id"]}, {
                "status": "consumed", "decided_at": decided["decided_at"], "approved_by": alice["id"],
                "approver_grants_version": pending["principal"][(alice["id"],)]["grants_version"],
            })]},
            "operation_outcome": {"added": [{"key": key, "company_id": company, "approval_id": card["approval_id"],
                "card_digest": card["digest"], "status": "committed"}]},
            "audit_log": {"added": [
                {"action": "submit_invoice", "actor": "alice", "company_id": company},
                {"action": "commit:create_invoice", "actor": "alice", "company_id": company,
                 "approval_id": card["approval_id"]},
            ]},
        })
        operation_path = f"/v1/operations/{key}"
        readback = client.request("GET", operation_path).json()
        check(readback == {field: result[field] for field in ("status", "text", "amounts", "currency")})
        assert_books(committed, books(world.owner))
        replay = client.request("POST", approve_path, json={"digest": card["digest"]})
        check(replay.json() == approved.json())
        assert_books(committed, books(world.owner))
        check([(method, path) for method, path, _ in observed] == [
            ("POST", "/v1/session"), ("POST", "/v1/conversations"), ("POST", message_path),
            ("POST", message_path),
            ("POST", approve_path), ("GET", operation_path), ("POST", approve_path),
        ])
        check(len(steps) == 2 + len(observed))


@pytest.mark.parametrize("fault", ["status", "bootstrap_body", "session_digest", "book_diff"])
def test_bootstrap_failure_diagnostics_omit_credentials(walking_world, monkeypatch, fault, request, capsys):
    __tracebackhide__ = True
    host_request = HostClient.request
    compare_books = assert_changes
    issued_credentials = []
    status = 201 if fault == "status" else 200

    def fail_book_diff(*args, **kwargs):
        __tracebackhide__ = True
        compare_books({}, {"principal_session": {(0,): {
            "session_secret": issued_credentials[1], "sign_in_code": issued_credentials[0],
        }}}, {})

    def unexpected_digest(secret):
        __tracebackhide__ = True
        return "unexpected"

    def unexpected_response(self, method, path, **kwargs):
        __tracebackhide__ = True
        response = host_request(self, method, path, **kwargs)
        if method == "POST" and path == "/v1/session":
            issued_credentials.extend([
                Concealed(kwargs["json"]["sign_in_code"]), Concealed(response.json()["session_secret"]),
            ])
            if fault == "status":
                return replace(response, status=status)
            if fault == "bootstrap_body":
                return replace(response, body=json.dumps({**response.json(), "name": "unexpected"}).encode())
            if fault == "session_digest":
                monkeypatch.setattr(pr, "session_digest", unexpected_digest)
            if fault == "book_diff":
                monkeypatch.setitem(globals(), "assert_changes", fail_book_diff)
        return response

    monkeypatch.setattr(HostClient, "request", unexpected_response)
    with pytest.raises(ClientError if fault == "status" else AssertionError) as failure:
        test_a_invoice_walking_skeleton(walking_world)
    if len(issued_credentials) != 2:
        raise AssertionError("Bootstrap did not issue both credentials") from None

    def check_report(report, style):
        __tracebackhide__ = True
        for credential in issued_credentials:
            if credential in report:
                raise AssertionError(f"{style} traceback exposed a bootstrap credential") from None

    check_report(str(failure.value), "exception")
    for style in ("long", "short", "native"):
        check_report(str(failure.getrepr(style=style, showlocals=True, funcargs=True)), style)
    expected = f"POST /v1/session: HTTP {status}" + (" (AssertionError)" if fault == "status" else "")
    if str(failure.value) != expected:
        raise AssertionError(expected) from None

    class BootstrapFailure:
        @pytest.fixture
        def bootstrap_failure(self):
            __tracebackhide__ = True

            def fail():
                __tracebackhide__ = True
                # Reuse the real failing frames without another database exchange.
                raise failure.value.with_traceback(failure.value.__traceback__) from None

            return fail

    # Pytester changes cwd; defer it until the Unix socket has closed.
    pytester = request.getfixturevalue("pytester")
    pytester.makepyfile('''
def test_bootstrap_failure(bootstrap_failure):
    bootstrap_failure()
''')
    try:
        result = pytester.runpytest_inprocess(
            "-q", "-p", "pytester", "--tb=long", "--showlocals", plugins=[BootstrapFailure()],
        )
    finally:
        # Pytester echoes its report; discard it before an outer failure can publish it.
        capsys.readouterr()
    check_report(result.stdout.str(), "pytest stdout")
    check_report(result.stderr.str(), "pytest stderr")
    result.assert_outcomes(failed=1)
    assert f"POST /v1/session: HTTP {status}" in result.stdout.str()


@pytest.mark.skip(reason=(
    "Missing conversation CARD stream route in create_app and host LISTENER_TABLES; "
    "POST /v1/conversations/{cid}/messages currently returns cards in its response only."
))
def test_the_card_arrives_on_the_stream():
    """Fill this companion when the engine registers its conversation card stream route."""
