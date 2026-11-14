"""The catalogue id of a refusal survives every way it leaves: a plugin these books have not enabled, asked for
directly or approved after it was disabled, and a refusal raised inside an approved commit, as returned, as read
back from its stored outcome, and as the API's event."""
from __future__ import annotations

from fastapi.testclient import TestClient

from erpclaw_api.app import create_app
from erpclaw_assistant import store
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import approvals
from erpclaw_core.common import Refusal
from erpclaw_core.plugins import REGISTRY, disable_plugin

INVOICE = call("create_invoice", customer="Wayne Enterprises", items=[{"item": "circuit board", "quantity": 2}])
ARGS = {"customer": "Wayne Enterprises", "items": [{"item": "circuit board", "quantity": 2}]}
SAID = "invoice Wayne Enterprises for 2 circuit boards"  # every value the card takes, in the owner's words


def client(engine, replies=()):
    c = TestClient(create_app(engine, ScriptedModel(list(replies)), embedded_owner=True))
    return c, c.post("/v1/conversations").json()["id"]


def card(engine):
    c, cid = client(engine, [Reply("", [INVOICE])])
    [ev] = c.post(f"/v1/conversations/{cid}/messages", json={"text": SAID}).json()["events"]
    assert ev["type"] == "card", ev
    return c, ev


def approve(c, ev):
    return c.post(f"/v1/cards/{ev['approval_id']}/approve", json={"digest": ev["digest"]}).json()["events"]


# --- a plugin these books have not enabled ----------------------------------------------------------------------

def test_an_operation_of_a_disabled_plugin_called_directly_names_it_as_available(engine, as_released):
    with engine.begin() as conn:
        disable_plugin(conn, "sales")
    c, cid = client(engine, [Reply("", [INVOICE])])
    [e] = c.post(f"/v1/conversations/{cid}/messages", json={"text": SAID}).json()["events"]
    assert e["type"] == "refused" and e["code"] == "plugin_not_enabled"
    assert e["capability"] == "available.op.create_invoice"


def test_proposing_an_operation_of_a_disabled_plugin_names_it_as_available(engine):
    with engine.begin() as conn:
        disable_plugin(conn, "sales")
    try:
        approvals.propose(engine, REGISTRY.contracts["create_invoice"], ARGS)
    except Refusal as r:
        assert r.code == "plugin_not_enabled" and r.capability == "available.op.create_invoice"
    else:
        raise AssertionError("no refusal")


def test_approving_a_card_after_its_plugin_was_disabled_names_it_as_available(engine, as_released):
    c, ev = card(engine)
    with engine.begin() as conn:
        disable_plugin(conn, "sales")
    [e] = approve(c, ev)
    assert e["status"] == "superseded" and e["code"] == "plugin_not_enabled"
    assert e["capability"] == "available.op.create_invoice"


# --- a refusal raised inside an approved commit -----------------------------------------------------------------

def refuse_in_commit(monkeypatch, refusal):
    def commit(ctx, inp, key):
        raise refusal

    monkeypatch.setattr(REGISTRY.contracts["create_invoice"], "commit", commit)


def test_a_refusal_inside_the_commit_keeps_its_id_returned_read_back_and_sent(engine, as_released, monkeypatch):
    c, ev = card(engine)
    refuse_in_commit(monkeypatch, Refusal("Emailing an invoice is off.", "planned", capability="planned.messaging"))
    [e] = approve(c, ev)  # the API's event
    assert e["type"] == "result" and e["status"] == "refused"
    assert e["code"] == "planned" and e["capability"] == "planned.messaging"
    again = approve(c, ev)  # the same card again: replayed from its stored outcome
    assert again == [e]
    cid = approvals.conversation_of(engine, ev["approval_id"])
    owner = store.conversation_owner(cid)
    back = approvals.read_back(engine, e["operation_key"], ownership_check=lambda target: store.conversation_owner(target) == owner)
    assert back.status == "refused" and back.data["code"] == "planned" and back.data["capability"] == "planned.messaging"


def test_the_outcome_approve_returns_keeps_the_id(engine, as_released, monkeypatch):
    out = approvals.propose(engine, REGISTRY.contracts["create_invoice"], ARGS)
    refuse_in_commit(monkeypatch, Refusal("Emailing an invoice is off.", "planned", capability="planned.messaging"))
    o = approvals.approve(engine, out.approval_id, out.digest)
    assert o.status == "refused" and o.data["code"] == "planned" and o.data["capability"] == "planned.messaging"


def test_a_refusal_inside_the_commit_without_an_id_still_sends_its_code(engine, as_released, monkeypatch):
    c, ev = card(engine)
    refuse_in_commit(monkeypatch, Refusal("The customer is on hold.", "customer_on_hold"))
    [e] = approve(c, ev)
    assert e["type"] == "result" and e["status"] == "refused" and e["code"] == "customer_on_hold"
    assert "capability" not in e
