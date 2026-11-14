"""Every assistant-store write screens its complete value before persistence."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import secrets
import sqlite3

import pytest
from sqlalchemy import select

from erpclaw_assistant import store
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import credentials
from erpclaw_core import approvals, models as m
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.db import EVAL_MARK, mark_demo
from erpclaw_core.masters import create_party

from test_model_profiles import FakeServer, LMSTUDIO, OPENROUTER, completion, model
from test_no_stored_credentials_chat import absent, assert_refusal, probe, route

SENTENCE = "(withheld: it held a credential)"


def credential():
    return "".join(("sk", "-or-", "v1-", secrets.token_hex(24)))


def dump():
    with sqlite3.connect(store.path()) as conn:
        return "\n".join(conn.iterdump())


def start():
    c = store.claim("boundary", "hello", principal_id="owner-a", principal_name="Alice")
    req = store.new_request(c)
    assert req and store.assign(c, req["id"])
    return c, req["id"]


def assert_withheld(value, original):
    text = json.dumps(value)
    assert original not in text
    assert original[-20:] not in text
    assert "openrouter_key" in text and SENTENCE in text
    assert "surrounding words" not in text


@pytest.mark.parametrize("choice_kind", ["credential", "unoffered", "stale"])
def test_unoffered_choice_refuses_without_storing_any_of_the_turn(engine, choice_kind):
    a = Assistant(engine, ScriptedModel([Reply("", [call("create_invoice", items=[{"item": "copper wire", "quantity": 5}])])]))
    a.turn("choices", "Invoice for 5 copper wire")
    before = dump()
    choice = credential() if choice_kind == "credential" else ("q999-1" if choice_kind == "stale" else "not offered")
    result = a.turn("choices", "surrounding words", choice=choice)
    assert result == [{"type": "refused", "code": "credential_refused", "text": credentials.CREDENTIAL_REFUSED,
                       "currency": "USD"}]
    assert dump() == before
    assert choice not in json.dumps(result)


def test_choice_is_checked_again_under_the_claim_transaction_and_valid_retries_replay():
    c, rid = start()
    store.set_pending(rid, question={"choices": [{"code": "q2-1"}], "event": {"choices": [{"code": "q2-1"}]}})
    store.release(c)
    before = dump()
    with pytest.raises(Refusal) as exc:
        store.claim("boundary", "old answer", "q1-1", principal_id="owner-a", principal_name="Alice",
                    choice_codes={"q1-1"})
    assert exc.value.code == "credential_refused"
    assert dump() == before
    accepted = store.claim("boundary", "current answer", "q2-1", principal_id="owner-a", principal_name="Alice",
                           choice_codes={"q2-1"}, client_message_id="accepted")
    assert isinstance(accepted, store.Claim)
    assert store.assign(accepted, rid)
    store.add_events(rid, [{"type": "answer", "text": "done"}], turn_id=accepted.turn_id)
    store.set_pending(rid)
    store.release(accepted)
    retry = store.claim("boundary", "current answer", "q2-1", principal_id="owner-a", principal_name="Alice",
                        choice_codes=set(), client_message_id="accepted")
    assert isinstance(retry, store.ClaimResult) and retry.kind == "replay"
    assert retry.events == [{"type": "answer", "text": "done"}]


@pytest.mark.parametrize("hosted", [False, True], ids=["local", "hosted"])
def test_real_client_reply_cannot_reach_store_or_returned_events(engine, hosted):
    value = credential()
    fake = FakeServer(completion("surrounding words " + value))
    if hosted:
        mark_demo(engine, EVAL_MARK)
    mdl = model(OPENROUTER if hosted else LMSTUDIO, fake)
    try:
        result = Assistant(engine, mdl).turn("reply", "hello")
        assert len(fake.bodies) == 1
        assert_refusal(result)
        absent(value, result)
        absent(value, dump())
        assert value not in repr(mdl.usage)
        assert result[-1]["text"] == store.events(store.history_page("reply")["items"][0]["request_id"])[-1]["text"]
    finally:
        mdl.client.close()


@pytest.mark.parametrize("writer", ["claim", "demand", "events", "pending", "bindings", "resolution", "outcome"])
def test_direct_writers_withhold_whole_text_or_json_values(writer):
    value = credential()
    text = "surrounding words " + value
    c, rid = start()
    if writer == "claim":
        store.release(c)
        store.claim("boundary", text, principal_id="owner-a", principal_name=text)
    elif writer == "demand":
        store.record_demand("company", "e", "feature", text)
    elif writer == "events":
        events = [{"type": "answer", "text": "surrounding words", "nested": [{"value": value}]}]
        store.add_events(rid, events)
        assert store.events(rid) == [{"type": "refused", "code": "credential_refused",
                                      "text": "openrouter_key " + SENTENCE}]
        assert events == store.events(rid)
    elif writer == "pending":
        store.set_pending(rid, question={"text": "surrounding words", "nested": [{"value": value}]})
        assert store.get_request(rid)["pending_question"] == {"text": "openrouter_key " + SENTENCE}
    elif writer == "bindings":
        assert store.finalise(c, rid, [], pending_question=None, pending_card=None, state="open", why=None,
                              serial_now=0, bindings_now={"memo": {"safe": "surrounding words", "nested": [value]}},
                              intent_state="unused")
        assert store.bindings(rid) == {"memo": {"text": "openrouter_key " + SENTENCE}}
    elif writer == "resolution":
        resolution = {"owner_messages": [{"id": c.message_id, "question_id": text, "field": text,
                       "choice_label": text, "spans": [{"id": "span", "start": 0, "end": 5,
                       "kind": text, "record_id": text, "name": text, "area": text,
                       "question": {"nested": value}, "classification": None}]}],
                      "pending": {"safe": "surrounding words", "nested": [value]}, "bindings": {"memo": value}}
        assert store.save_resolution(c, rid, resolution)
    else:
        assert store.apply_outcome("card", "committed", rid, text)
    assert_withheld(dump(), value)


@pytest.mark.parametrize("hosted_route", [False, True], ids=["local", "hosted"])
def test_invalid_credential_choice_and_retry_leave_pending_turn_unwritten(engine, hosted_route):
    mdl = route(engine, hosted_route)
    mdl.replies = [Reply("", [call("create_invoice", items=[{"item": "copper wire", "quantity": 5}])])]
    assistant = Assistant(engine, mdl)
    assistant.turn("invalid-choice", "Invoice for 5 copper wire")
    pending = store.open_request("invalid-choice")
    assert pending and pending["pending_question"]
    before, sent_before = dump(), list(mdl.sent)
    value = probe("assignment")
    for _ in range(2):
        assert_refusal(assistant.turn("invalid-choice", value, choice=value, client_message_id="invalid"))
        assert dump() == before
        assert store.open_request("invalid-choice") == pending
        assert mdl.sent == sent_before


@pytest.mark.parametrize("event_type", ["answer", "ask", "card", "notice", "result"])
def test_withheld_event_is_a_fixed_refusal_printed_by_cli_and_replayed(monkeypatch, event_type):
    from rich.console import Console
    from erpclaw_cli import main

    value = credential()
    c = store.claim("boundary", "hello", principal_id="owner-a", principal_name="Alice", client_message_id="withheld")
    req = store.new_request(c)
    assert req and store.assign(c, req["id"])
    rid = req["id"]
    events = [{"type": event_type, "id": value, "approval_id": value,
               "text": "surrounding words", "nested": [{"value": value}]}]
    assert store.finalise(c, rid, events, pending_question=None, pending_card=None, state="ended", why="done",
                          serial_now=0, bindings_now={}, intent_state="unused")
    expected = [{"type": "refused", "code": "credential_refused", "text": "openrouter_key " + SENTENCE}]
    assert events == store.events(rid) == expected
    retry = store.claim("boundary", "hello", principal_id="owner-a", principal_name="Alice", client_message_id="withheld")
    assert isinstance(retry, store.ClaimResult) and retry.kind == "replay"
    assert retry.events == expected
    console = Console(record=True, width=200, color_system=None)
    monkeypatch.setattr(main, "con", console)
    assert main.show_events(None, "boundary", events) is None
    assert console.export_text().strip() == expected[0]["text"]
    assert_withheld(dump(), value)


def test_legacy_card_reconciliation_withholds_event_and_releases_claim(engine, monkeypatch):
    from rich.console import Console
    from erpclaw_cli import main

    assistant = Assistant(engine, ScriptedModel([Reply("", [call(
        "create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 2}])])]))
    cid = "legacy-credential"
    card = assistant.turn(cid, "Invoice Acme Corporation for 2 copper wire")[-1]
    assert card["type"] == "card"
    rid = store.open_request(cid)["id"]
    with engine.begin() as conn:
        conn.execute(m.party.update().where(m.party.c.name == "Acme Corporation").values(name="Acme Corporation North"))
        create_party(company_ctx(conn), "customer", "Acme Corporation South")
    assert approvals.approve(engine, card["approval_id"], card["digest"], ownership_check=_ownership(engine)).status == "superseded"
    value = credential()
    with engine.begin() as conn:
        legacy = json.loads(conn.execute(select(m.approval.c.card_json).where(
            m.approval.c.id == card["approval_id"])).scalar_one())
        legacy["title"] = "surrounding words " + value
        conn.execute(m.approval.update().where(m.approval.c.id == card["approval_id"]).values(card_json=json.dumps(legacy)))
    result = assistant.turn(cid, "please continue", client_message_id="reconcile")
    assert [e["type"] for e in result] == ["refused", "ask"]
    assert result[0]["code"] == "credential_refused"
    assert result[0]["text"] == "openrouter_key " + SENTENCE
    assert store.outcome_applied(card["approval_id"], "superseded")
    assert store.open_request(cid)["pending_card"] is None
    assert store.open_request(cid)["pending_question"] is not None
    assert assistant.turn(cid, "please continue", client_message_id="reconcile") == result
    with sqlite3.connect(store.path()) as conn:
        claim = conn.execute("SELECT claimed_by, claim_until, turn_deadline_at FROM conversation WHERE id = ?", (cid,)).fetchone()
    assert claim == (None, None, None)
    console = Console(record=True, width=200, color_system=None)
    monkeypatch.setattr(main, "con", console)
    assert main.show_events(None, cid, result) == result[-1]
    assert result[0]["text"] in console.export_text()
    for held in (result, store.events(rid), store.timeline(rid), store.history_page(cid), dump(), console.export_text()):
        assert value not in json.dumps(held)
        assert value[-20:] not in json.dumps(held)


@pytest.mark.parametrize("kind", ["encoded-field", "nested", "multiple", "unreadable"])
def test_store_helper_checks_complete_json_and_retains_only_labels(kind):
    value = credential()
    if kind == "encoded-field":
        original = '{"safe": "surrounding words", "\\u0070' + 'assword": "' + secrets.token_hex(12) + '"}'
        label = "password_assignment"
    elif kind == "nested":
        original = json.dumps({"safe": "surrounding words", "nested": json.dumps({"value": value})})
        label = "openrouter_key"
    elif kind == "multiple":
        original = json.dumps({"safe": "surrounding words", "nested": [value, "pass" + "word = " + secrets.token_hex(12)]})
        label = "openrouter_key password_assignment"
    else:
        original = "{" * 5000
        label = "unreadable_json"
    store.record_demand("company", "e", "feature", original)
    expected = label + " " + SENTENCE
    actual = store.demand()[0][-1]
    if kind != "unreadable":
        assert json.loads(actual) == {"text": expected}
    else:
        assert actual == expected
    assert original not in dump()


def unguarded_writes(source):
    """Conservatively check every bound value, including ids and non-text columns.

    SQL is found from the call's syntax, so adding another writer or another column
    requires no writer allowlist. Literal SQL values are checked too. Executemany
    rows must pass the same check as a single execute's tuple.
    """
    tree = ast.parse(source)
    failures = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(function):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"execute", "executemany", "executescript"} and node.args):
                continue
            if node.func.attr == "executescript":
                failures.append(f"{function.name}:{node.lineno}")  # scripts cannot bind screened values
                continue
            sql_node = node.args[0]
            if isinstance(sql_node, ast.Constant) and isinstance(sql_node.value, str):
                sql = sql_node.value
            elif isinstance(sql_node, ast.JoinedStr):
                sql = "".join(x.value if isinstance(x, ast.Constant) else "{dynamic}" for x in sql_node.values)
            else:
                # The only indirect SQL is the schema DDL loop, checked below.
                ddl = function.name == "_open" and isinstance(sql_node, ast.Name) and sql_node.id == "ddl"
                expression = sql_node
                while isinstance(expression, ast.Call) and isinstance(expression.func, ast.Attribute):
                    expression = expression.func.value
                select = (isinstance(expression, ast.Call) and isinstance(expression.func, ast.Name)
                          and expression.func.id == "select")
                if not (ddl or select):
                    failures.append(f"{function.name}:{node.lineno}")
                continue
            if not re.match(r"\s*(INSERT|UPDATE)\b", sql, re.I):
                continue
            # Interpolating a table identifier is allowed; interpolating a value bypasses binding.
            identifiers = re.sub(r"(?i)^(UPDATE|INSERT(?: OR \w+)? INTO) \{dynamic\} ", r"\1 table ", sql)
            bad = "{dynamic}" in identifiers
            if len(node.args) > 1:
                params = node.args[1]
                if node.func.attr == "executemany":
                    params = params.elt if isinstance(params, (ast.ListComp, ast.GeneratorExp)) else None
                bad |= not (isinstance(params, ast.Tuple) and all(
                    isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "_stored"
                    for v in params.elts))
            else:
                bad = True  # literal and copied text must also go through the helper
            if any(credentials.check(v.replace("''", "'")) for v in re.findall(r"'((?:''|[^'])*)'", sql)):
                bad = True
            if bad:
                failures.append(f"{function.name}:{node.lineno}")
    # A dynamic SQL argument must come only from the schema DDL lists.
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id.startswith("_V") for t in node.targets):
            assert not re.search(r"\b(INSERT|UPDATE)\b", ast.unparse(node.value), re.I)
    return failures


def test_all_store_sql_writes_screen_every_bound_value():
    source = Path(store.__file__).read_text()
    assert unguarded_writes(source) == []


@pytest.mark.parametrize("operation", ["INSERT INTO capability_demand (request) VALUES (?)",
                                       "UPDATE capability_demand SET request = ?"])
def test_structural_guard_rejects_a_new_bypassing_writer(operation):
    source = Path(store.__file__).read_text()
    mutation = source + f'\n\ndef added_writer(conn, text):\n    conn.execute({operation!r}, (text,))\n'
    assert any(item.startswith("added_writer:") for item in unguarded_writes(mutation))


def test_structural_guard_rejects_indirect_sql_and_literal_writes():
    for body in ('sql = "UPDATE capability_demand SET request = ?"\n    conn.execute(sql, (text,))',
                 'conn.execute("UPDATE capability_demand SET request = \'unchecked\'")',
                 'conn.executescript("UPDATE capability_demand SET request = \'unchecked\'")',
                 '''conn.execute(f"UPDATE capability_demand SET request = '{text}' WHERE outcome = ?", (_stored("e"),))'''):
        mutation = Path(store.__file__).read_text() + '\n\ndef added_writer(conn, text):\n    ' + body + '\n'
        assert any(item.startswith("added_writer:") for item in unguarded_writes(mutation))


def _ownership(engine):
    from erpclaw_assistant import store
    from erpclaw_core import principals

    with engine.connect() as conn:
        owner = principals.owner(conn)["id"]
    return lambda cid: store.conversation_owner(cid) == owner
