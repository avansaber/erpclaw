"""Pending-card routing honors the owner's words, regardless of the model's call."""
import pytest

from conftest import PG_URL
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import approvals
from erpclaw_cli.evals.run import EvalBooks
from erpclaw_cli.evals.seeds import services_co


@pytest.fixture(scope="module")
def books():
    b = EvalBooks(PG_URL)
    try:
        b.seed(services_co)
        yield b
    finally:
        b.close()


@pytest.fixture
def trial(books, monkeypatch):
    monkeypatch.setenv("ERPCLAW_TODAY", "2026-11-03")
    eng = books.trial()
    yield eng
    books.finish(eng)


@pytest.mark.parametrize("message,reply", [
    ("yes approve it", Reply("", [call("submit_invoice", invoice="INV-2026-00001")])),
    ("yes approve it", Reply("Okay.")),
    ("approve it", Reply("", [call("submit_invoice", invoice="INV-2026-00001")])),
])
def test_typed_approval_preserves_card(trial, message, reply):
    invoice = call("create_invoice", customer="Initech", items=[{"item": "Support retainer", "quantity": 1}], submit=True)
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), reply]))
    [card] = a.turn("approval_gate", "Invoice Initech for one support retainer.")
    assert card["type"] == "card"
    events = a.turn("approval_gate", message)
    assert [e["type"] for e in events] == ["answer"], events
    assert events[0]["text"] == "Nothing has been saved yet. Approve the card to save it."
    assert approvals.pending_card(trial, "approval_gate")["id"] == card["approval_id"]
    assert approvals.approve(trial, card["approval_id"], card["digest"]).status == "committed"


@pytest.mark.parametrize("message,expected", [("Wayne Enterprises", "Wayne Enterprises"),
                                               ("2", "2")])
def test_field_value_overrides_unrelated_model_call(trial, message, expected):
    invoice = call("create_invoice", customer="Initech", items=[{"item": "Support retainer", "quantity": 1}], submit=True)
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), Reply("", [call("list_customers")])]))
    [card] = a.turn("field_gate", "Invoice Initech for one support retainer.")
    events = a.turn("field_gate", message)
    assert events[0]["code"] == "card_set_aside", events
    assert events[-1]["type"] in ("card", "ask") and expected in str(events[-1]), events
    assert approvals.approve(trial, card["approval_id"], card["digest"]).status == "superseded"
