"""Credential-bearing proposals and mail write nothing; commit refusals keep a safe replayable outcome."""
from __future__ import annotations

import json
import secrets
from dataclasses import replace

import pytest
from sqlalchemy import select

import bookdiff
from erpclaw_core import approvals, models as m
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.outbox import SenderSnapshot, enqueue
from erpclaw_core.plugins import REGISTRY
from test_no_stored_credentials_chat import REFUSED_CODE, REFUSED_TEXT, absent, probe


# Real contracts and valid counterparts, including text beside a money figure.
CASES = (
    ("add_customer", {"name": "Plain Customer"}, "name"),
    ("add_customer", {"name": "Plain Customer", "email": "billing@example.test"}, "email"),
    ("add_customer", {"name": "Plain Customer", "city": "Plain City"}, "city"),
    ("add_supplier", {"name": "Plain Supplier"}, "name"),
    ("add_item", {"name": "Plain Service", "unit": "hour", "price": "25.00", "kind": "service"}, "name"),
    ("record_payment", {"customer": "Acme Corporation", "amount": "12.00", "reference": "Plain reference"}, "reference"),
    ("set_up_company", {"name": "Plain Books", "currency": "USD", "fiscal_year_start_month": 1,
                        "date_order": "MDY"}, "name"),
)


def all_books(engine):
    return json.dumps({t: list(rows.values()) for t, rows in bookdiff.snapshot(engine).items()}, default=str)


@pytest.mark.parametrize("name,args,field", CASES, ids=[f"{n}-{f}" for n, _, f in CASES])
def test_propose_path_refuses_shaped_text_before_any_row(engine, name, args, field):
    if name == "set_up_company":
        with engine.begin() as conn:
            conn.execute(m.company.update().values(status="pending"))
    value = probe("assignment")
    # A syntactically valid address: scanning must happen before the field validator too.
    if field == "email":
        value = "pass" + "word=" + secrets.token_hex(6) + "@example.test"
    before = bookdiff.snapshot(engine)
    with pytest.raises(Refusal) as exc:
        approvals.propose(engine, REGISTRY.contracts[name], args | {field: value})
    assert (exc.value.code, exc.value.message) == (REFUSED_CODE, REFUSED_TEXT)
    bookdiff.assert_changes(before, bookdiff.snapshot(engine), {})
    absent(value, all_books(engine))
    assert approvals.propose(engine, REGISTRY.contracts[name], args).status == "card"


@pytest.mark.parametrize("where", ("owner_words", "preview"))
def test_proposal_checks_reassembled_words_and_preview(engine, monkeypatch, where):
    contract = REGISTRY.contracts["add_customer"]
    value = probe("quoted-assignment")
    kw = {"owner_words": "words " + value} if where == "owner_words" else {}
    if where == "preview":
        original = contract.preview

        def preview(ctx, inp):
            result = original(ctx, inp)
            result.card.lines.append(("Note", value))
            return result

        monkeypatch.setattr(contract, "preview", preview)
    before = bookdiff.snapshot(engine)
    with pytest.raises(Refusal) as exc:
        approvals.propose(engine, contract, {"name": "Plain Customer"}, **kw)
    assert (exc.value.code, exc.value.message) == (REFUSED_CODE, REFUSED_TEXT)
    bookdiff.assert_changes(before, bookdiff.snapshot(engine), {})


@pytest.mark.parametrize("source", ("stored_args", "enqueue", "preview"))
def test_commit_time_refusal_replays_without_audit_value(engine, monkeypatch, source):
    contract = REGISTRY.contracts["add_customer"]
    ordinary = approvals.propose(engine, contract, {"name": "Ordinary Customer"})
    assert approvals.approve(engine, ordinary.approval_id, ordinary.digest).status == "committed"
    outcome = approvals.propose(engine, contract, {"name": "Another Customer", "new_customer": True})
    value = probe("private-key-body")
    if source == "stored_args":
        with engine.begin() as conn:
            conn.execute(m.approval.update().where(m.approval.c.id == outcome.approval_id).values(
                args_json=json.dumps({"name": "plain prefix " + value})))
    elif source == "enqueue":
        original = contract.commit

        def commit(ctx, inp, key):
            result = original(ctx, inp, key)  # business row and audit must roll back too
            enqueue(ctx, "test-slot", "billing@example.test", "Plain subject", value, key)
            return result

        monkeypatch.setattr(contract, "commit", commit)
    else:
        original = contract.preview

        def preview(ctx, inp):
            result = original(ctx, inp)
            result.card.lines.append(("Note", value))
            return result

        monkeypatch.setattr(contract, "preview", preview)
    before = bookdiff.snapshot(engine)
    result = approvals.approve(engine, outcome.approval_id, outcome.digest)
    assert result.status == "refused" and result.data["code"] == REFUSED_CODE
    assert result.text == "Nothing was saved: " + REFUSED_TEXT
    assert result.operation_key == outcome.operation_key
    from fastapi.testclient import TestClient
    from erpclaw_api.app import create_app
    from erpclaw_assistant.model import ScriptedModel
    client = TestClient(create_app(engine, ScriptedModel([]), embedded_owner=True))
    response = client.post(f"/v1/cards/{outcome.approval_id}/approve", json={"digest": outcome.digest})
    assert response.status_code == 200
    event = response.json()["events"][-1]
    assert event["type"] == "result" and event["status"] == "refused"
    assert event["code"] == REFUSED_CODE and event["text"] == result.text
    assert event["operation_key"] == outcome.operation_key and event["currency"] == "USD"
    after = bookdiff.snapshot(engine)
    changes = bookdiff.changes(before, after)
    assert set(changes) == {"approval", "operation_outcome"}
    assert len(changes["operation_outcome"]["added"]) == 1
    assert changes["operation_outcome"]["added"][0]["status"] == "refused"
    # A planted legacy card already contains the value. It is never copied into new/changed fields.
    for table, diff in changes.items():
        absent(value, json.dumps(diff["added"], default=str))
        for old, new in diff["changed"]:
            absent(value, json.dumps({k: v for k, v in new.items() if v != old[k]}, default=str))
    with engine.connect() as conn:
        absent(value, conn.execute(select(m.audit_log.c.detail)).all())
    for again in (approvals.approve(engine, outcome.approval_id, outcome.digest),
                  approvals.read_back(engine, outcome.operation_key)):
        assert again.status == result.status and again.text == result.text
        assert again.operation_key == result.operation_key and again.data["code"] == REFUSED_CODE
    assert bookdiff.snapshot(engine) == after


@pytest.mark.parametrize("field", ("recipient", "subject", "body", "from_address", "from_name", "reply_to"))
def test_outbox_enqueue_refuses_before_queueing(engine, field):
    value = probe("encoded-json")
    fields = {"recipient": "billing@example.test", "subject": "Plain subject", "body": "Plain body"}
    sender = SenderSnapshot(from_address="sender@example.test", from_name="Plain Sender", reply_to="reply@example.test", read_entry=None)
    if field in fields:
        fields[field] = value
    else:
        sender = replace(sender, **{field: value})
    before = bookdiff.snapshot(engine)
    with engine.begin() as conn:
        with pytest.raises(Refusal) as exc:
            enqueue(company_ctx(conn), "test-slot", **fields, operation_key=None, sender=sender)
        assert (exc.value.code, exc.value.message) == (REFUSED_CODE, REFUSED_TEXT)
    bookdiff.assert_changes(before, bookdiff.snapshot(engine), {})
    absent(value, all_books(engine))
    with engine.begin() as conn:
        assert enqueue(company_ctx(conn), "test-slot", "billing@example.test", "Plain subject", "Plain body", None)
