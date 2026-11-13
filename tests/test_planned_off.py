"""Emailing, the invoice list, reports and supplier and goods reversals are switched off in this release;
customer payments, cancelling and reversing are on, and are switched off here to test the same path on them.

Each is refused as planned for its release wherever it is asked for, and the books do not change: a book diff
of every table is empty. Each comes back on through its own specification, review and build.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal

from sqlalchemy import select

import pytest
from fastapi.testclient import TestClient

from bookdiff import assert_changes, snapshot
from erpclaw_api.app import create_app
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import approvals
from erpclaw_core.common import Refusal
from erpclaw_core.plugins import REGISTRY

OFF = {  # each switched-off operation, called as the model would call its tool
    "payment": call("record_payment", customer="Pied Piper", amount="100", oldest_first=True, method="check"),
    "cancel": call("cancel_invoice", invoice="INV-2026-00001"),
    "invoice list": call("list_invoices"),
    "figures": call("get_metric", metric="receivables"),
}
PLANNED = "not in ERPClaw yet."
PAYMENTS = ("record_payment", "cancel_invoice", "reverse_payment")
REVERSALS = [
    ("reverse_supplier_payment", {"payment": "SPAY-2026-00001"}, "later"),
    ("reverse_goods_received", {"delivery": "GR-2026-00001"}, "v3.0"),
]


@pytest.fixture
def payments_off(as_released, monkeypatch):
    """The release, with payments, cancelling and reversing switched off as they were before they came on, so the
    switched-off path is still tested on them; it fails rather than let a test pass with them on."""
    for name in PAYMENTS:
        monkeypatch.setattr(REGISTRY.contracts[name], "planned", "Core")
    assert all(REGISTRY.contracts[n].planned == "Core" for n in PAYMENTS)


def planned(text: str) -> bool:
    """A planned reply, whether it names one thing or figures and reports."""
    assert "planned for" not in text
    return re.search(r"not in ERPClaw yet\.", text) is not None


def chat(engine, replies, said="do it"):
    c = TestClient(create_app(engine, ScriptedModel(list(replies)), embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    return c.post(f"/v1/conversations/{cid}/messages", json={"text": said}).json()["events"]


@pytest.mark.parametrize("what", sorted(OFF))
def test_a_switched_off_operation_called_directly_is_refused_as_planned_and_changes_nothing(engine, payments_off,
                                                                                             what):
    Assistant(engine, ScriptedModel([]))
    before = snapshot(engine)
    events = chat(engine, [Reply("", [OFF[what]])])
    assert [e.get("outcome") for e in events] == ["c"] and planned(events[0]["text"]), events
    assert events[0]["capability"].startswith("planned.")
    assert snapshot(engine) == before


@pytest.mark.parametrize("request_, entry", [
    ("record a payment from Pied Piper", "op.record_payment"),
    ("cancel invoice INV-2026-00001", "op.cancel_invoice"),
    ("who owes us money", "metric.receivables"),
    ("what were our sales this year", "metric.sales"),
])
def test_a_switched_off_operation_chosen_through_the_catalogue_is_planned_and_changes_nothing(
        engine, payments_off, request_, entry):
    from erpclaw_core import catalogue as cat

    with engine.connect() as conn:
        found = {e.id: e for e in cat.find_capability(conn, request_)}
    assert found[entry].state == "planned" and found[entry].wave == "Core"
    Assistant(engine, ScriptedModel([]))
    before = snapshot(engine)
    events = chat(engine, [Reply("", [call("find_capability", request=request_)]),
                           Reply("", [call("choose_capability", pick=entry)])], request_)
    assert [e.get("outcome") for e in events] == ["c"] and planned(events[0]["text"]), events
    assert events[0]["capability"].startswith("planned.")
    assert snapshot(engine) == before


@pytest.mark.parametrize("contract, args", [
    ("record_payment", {"customer": "Pied Piper", "amount": "100", "oldest_first": True, "method": "check"}),
    ("cancel_invoice", {"invoice": "INV-2026-00001"}),
])
def test_no_card_is_made_for_a_switched_off_write(engine, monkeypatch, contract, args):
    monkeypatch.setattr(REGISTRY.contracts[contract], "planned", "Core")  # switched off for the test
    before = snapshot(engine)
    with pytest.raises(Refusal, match=re.escape(PLANNED)):
        approvals.propose(engine, REGISTRY.contracts[contract], args)
    assert snapshot(engine) == before


@pytest.mark.parametrize("name, args, wave", REVERSALS)
@pytest.mark.parametrize("where", ["tool", "catalogue", "propose"])
def test_reversals_are_planned_wherever_asked_and_change_nothing(engine, as_released, name, args, wave, where):
    from erpclaw_core import catalogue as cat

    contract = REGISTRY.contracts[name]
    assert contract.planned == wave
    expected = f"{name.replace('_', ' ').capitalize()} is not in ERPClaw yet."
    entry = f"op.{name}"
    request_ = name.replace("_", " ")
    with engine.connect() as conn:
        entries = {e.id: e for e in cat.catalogue(conn)}
        assert (entries[entry].state, entries[entry].wave, entries[entry].target) == ("planned", wave, name)
    Assistant(engine, ScriptedModel([]))
    before = snapshot(engine)
    if where == "propose":
        with pytest.raises(Refusal) as err:
            approvals.propose(engine, contract, args)
        assert err.value.code == "planned" and str(err.value) == expected
    else:
        replies = ([Reply("", [call(name, **args)])] if where == "tool" else [
            Reply("", [call("find_capability", request=request_)]),
            Reply("", [call("choose_capability", pick=entry)]),
        ])
        model = ScriptedModel(replies)
        client = TestClient(create_app(engine, model, embedded_owner=True))
        cid = client.post("/v1/conversations").json()["id"]
        events = client.post(f"/v1/conversations/{cid}/messages", json={"text": request_}).json()["events"]
        assert len(events) == 1 and events[0]["outcome"] == "c", events
        assert events[0]["text"] == expected and events[0]["capability"] == f"planned.{entry}"
        assert model.tools_seen and all(name not in tools for tools in model.tools_seen)
    assert_changes(before, snapshot(engine), {})


@pytest.mark.parametrize("request_", ["reverse a supplier payment", "undo the payment to Acme Supplies"])
def test_supplier_reversal_words_get_the_supplier_reversal_reply(engine, as_released, request_):
    from erpclaw_core import catalogue as cat

    with engine.connect() as conn:
        candidates = cat.find_capability(conn, request_)
        named = cat.named_by_synonym(conn, request_)
        decision = cat.decide(candidates, "op.reverse_supplier_payment", named=named)
    assert decision.text == "Reverse supplier payment is not in ERPClaw yet."
    events = chat(engine, [Reply("", [call("find_capability", request=request_)]),
                           Reply("", [call("choose_capability", pick="op.reverse_supplier_payment")])], request_)
    assert len(events) == 1 and events[0]["text"] == decision.text
    assert events[0]["capability"] == "planned.op.reverse_supplier_payment"


@pytest.mark.parametrize("request_", ["reverse a supplier payment", "undo the payment to Acme Supplies"])
def test_direct_customer_reversal_call_answers_supplier_reversal_as_planned(engine, as_released, request_):
    from erpclaw_core import models as m
    from erpclaw_core.common import company_ctx
    from erpclaw_sales import service, tables

    with engine.begin() as conn:
        ctx = company_ctx(conn)
        customer = dict(conn.execute(select(m.party).where(m.party.c.name == "Hooli")).mappings().one())
        payment = service.record_payment(ctx, service.plan_payment(ctx, customer, Decimal("100"), ctx.today,
                                                                   oldest_first=True))
        number = conn.execute(select(tables.payment.c.number).where(tables.payment.c.id == payment["id"])).scalar_one()
    Assistant(engine, ScriptedModel([]))
    before = snapshot(engine)
    events = chat(engine, [Reply("", [call("reverse_payment", payment=number)])], request_)
    assert len(events) == 1 and events[0].get("outcome") == "c", events
    assert events[0]["text"] == "Reverse supplier payment is not in ERPClaw yet."
    assert events[0]["capability"] == "planned.op.reverse_supplier_payment"
    assert all(e["type"] != "card" for e in events)
    assert_changes(before, snapshot(engine), {})


@pytest.mark.parametrize("request_", ["reverse the payment from Hooli", "reverse a payment"])
def test_direct_customer_reversal_call_keeps_normal_resolution(engine, as_released, request_):
    events = chat(engine, [Reply("", [call("reverse_payment", payment="PAY-2026-09999")])], request_)
    assert len(events) == 1 and events[0]["type"] == "ask", events
    assert events[0].get("capability") != "planned.op.reverse_supplier_payment"


@pytest.mark.parametrize("request_", ["reverse the payment from Hooli", "reverse a payment"])
def test_customer_reversal_words_reach_the_customer_operation(engine, as_released, request_):
    from erpclaw_core import catalogue as cat

    with engine.connect() as conn:
        candidates = cat.find_capability(conn, request_)
        named = cat.named_by_synonym(conn, request_)
        decision = cat.decide(candidates, "op.reverse_payment", named=named)
    assert decision.outcome == "a" and decision.entry.target == "reverse_payment"


@pytest.mark.parametrize("request_", ["reverse a supplier payment", "undo the payment to Acme Supplies"])
def test_supplier_reversal_binding_comes_from_override_data(engine, as_released, tmp_path, monkeypatch, request_):
    from erpclaw_core import catalogue as cat

    with engine.connect() as conn:
        named = cat.named_by_synonym(conn, request_)
        assert any(e.id == "op.reverse_supplier_payment" and "op.reverse_payment" in e.overrides for e, _ in named)
        records = json.loads(cat.OVERRIDES.read_text())
        records = [r for r in records if r["target"] != "op.reverse_supplier_payment"]
        overrides = tmp_path / "overrides.json"
        overrides.write_text(json.dumps(records))
        monkeypatch.setattr(cat, "OVERRIDES", overrides)
        named = cat.named_by_synonym(conn, request_)
        assert not any(e.id == "op.reverse_supplier_payment" and e.overrides for e, _ in named)


@pytest.mark.parametrize("request_", ["reverse a supplier payment", "undo the payment to Acme Supplies"])
def test_direct_reversal_guard_uses_override_data(engine, as_released, tmp_path, monkeypatch, request_):
    from erpclaw_core import catalogue as cat

    with engine.connect() as conn:
        decision = cat.planned_instead(conn, "reverse_payment", request_)
        assert decision.capability == "planned.op.reverse_supplier_payment"
        records = [r for r in json.loads(cat.OVERRIDES.read_text()) if r["target"] != "op.reverse_supplier_payment"]
        overrides = tmp_path / "overrides.json"
        overrides.write_text(json.dumps(records))
        monkeypatch.setattr(cat, "OVERRIDES", overrides)
        assert cat.planned_instead(conn, "reverse_payment", request_) is None


@pytest.mark.parametrize("request_", ["undo the payment to", "reverse the supplier invoice"])
def test_supplier_reversal_binding_requires_complete_trigger_and_payment(engine, as_released, request_):
    from erpclaw_core import catalogue as cat

    with engine.connect() as conn:
        named = cat.named_by_synonym(conn, request_)
    assert not any(e.id == "op.reverse_supplier_payment" and e.overrides for e, _ in named)


def test_a_card_made_before_its_operation_was_switched_off_does_not_commit(engine, monkeypatch):
    card = approvals.propose(engine, REGISTRY.contracts["record_payment"],
                             {"customer": "Pied Piper", "amount": "100", "oldest_first": True, "method": "check"})  # on
    monkeypatch.setattr(REGISTRY.contracts["record_payment"], "planned", "Core")  # then switched off
    before = snapshot(engine)
    out = approvals.approve(engine, card.approval_id, card.digest)
    # it ends, visibly, rather than refusing on every approval until it expires
    assert out.status == "superseded" and PLANNED in out.text and out.data["code"] == "planned"
    after = snapshot(engine)
    ended = after["approval"][(card.approval_id,)]
    assert_changes(before, after, {"approval": {"changed": [({"id": card.approval_id}, {
        "status": "superseded", "decided_at": ended["decided_at"], "decision_json": ended["decision_json"]})]}})
    again = approvals.approve(engine, card.approval_id, card.digest)  # its stored decision, nothing written
    assert (again.status, again.text, again.data["code"]) == ("superseded", out.text, "planned")
    assert snapshot(engine) == after


def test_the_model_is_not_offered_the_switched_off_tools(engine, payments_off):
    from erpclaw_core.plugins import enabled_plugins

    with engine.connect() as conn:
        enabled = enabled_plugins(conn)
    offered = {c.name for c in REGISTRY.contracts.values() if REGISTRY.contract_plugin[c.name] in enabled
               and not c.planned}
    assert offered == {"set_up_company", "add_customer", "add_supplier", "add_cash_account", "add_item", "list_customers", "list_suppliers", "list_items", "create_invoice", "submit_invoice"}


def test_an_invoice_the_owner_asks_to_email_is_drafted_without_the_email_and_says_so(engine, as_released):
    """Through chat the resolution step removes the switched-off option and says so beside the card:
    the card is made, nothing is queued, and an API caller sending email=true is still refused whole (below)."""
    Assistant(engine, ScriptedModel([]))
    before = snapshot(engine)
    events = chat(engine, [Reply("", [call("create_invoice", customer="Wayne Enterprises", items=[
        {"item": "circuit board", "quantity": 2}], email=True)])],
        "invoice Wayne Enterprises for 2 circuit boards and email it")
    assert [e["type"] for e in events] == ["not_done", "card"] and events[0]["part"] == "email", events
    assert_changes(before, snapshot(engine), {"approval": {"added": [{"contract": "create_invoice",
                                                                      "status": "pending"}]}})


def test_an_api_caller_asking_to_email_an_invoice_is_refused_whole_and_saves_nothing(engine, as_released):
    before = snapshot(engine)
    with pytest.raises(Refusal, match=re.escape("Emailing an invoice is not in ERPClaw yet.")):
        approvals.propose(engine, REGISTRY.contracts["create_invoice"], {
            "customer": "Wayne Enterprises", "items": [{"item": "circuit board", "quantity": 2}], "email": True})
    assert snapshot(engine) == before


def test_an_ordinary_invoice_still_makes_its_card(engine, as_released):
    events = chat(engine, [Reply("", [call("create_invoice", customer="Wayne Enterprises", items=[
        {"item": "circuit board", "quantity": 2}])])], "invoice Wayne Enterprises for 2 circuit boards")
    assert [e["type"] for e in events] == ["card"]



def test_nothing_is_delivered_after_a_commit_while_delivery_is_off(engine, as_released):
    """The audit (pilot paths reachable from chat): the API delivered queued messages after every commit."""
    from sqlalchemy import select

    from erpclaw_core import models as m
    from erpclaw_core.common import company_ctx
    from erpclaw_core.outbox import enqueue

    with engine.begin() as conn:
        enqueue(company_ctx(conn), "deliver_message", "ap@wayne.example", "Hi", "Body", None)
    c = TestClient(create_app(engine, ScriptedModel([Reply("", [call("create_invoice", customer="Wayne Enterprises",
                                                                     items=[{"item": "circuit board", "quantity": 2}])])]),
                              embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    [card] = c.post(f"/v1/conversations/{cid}/messages",
                    json={"text": "invoice Wayne Enterprises for 2 circuit boards"}).json()["events"]
    events = c.post(f"/v1/cards/{card['approval_id']}/approve", json={"digest": card["digest"]}).json()["events"]
    assert events[0]["status"] == "committed" and not [e for e in events if e["type"] == "email"]
    with engine.connect() as conn:
        assert conn.execute(select(m.outbox.c.status)).scalars().all() == ["queued"]
