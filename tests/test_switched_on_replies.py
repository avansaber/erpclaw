"""The assistant's fallback replies point the owner only at what is switched on in these books.

When the model's answer holds a figure no tool gave, and when the assistant gives up, the reply says what can be
asked for instead: the engine's sentence built from the enabled operations' plain names, never a fixed list that
can name something switched off, and with no figure.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from erpclaw_api.app import create_app
from erpclaw_assistant.graph import MAX_MODEL_REQUESTS
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core.plugins import REGISTRY, disable_plugin

# the words for what this release switches off, the figures among them, as the old fixed lists used them
OFF_WORDS = ("sales", "money owed", "owes", "top customers", "invoice count", "list invoices", "record payments",
             "cancel an invoice", "reverse a payment", "figures and reports")
PAYMENTS = ("record_payment", "cancel_invoice", "reverse_payment")
ON_NOW = "Right now I can add customers, add items, draft invoices, and submit a draft invoice."


@pytest.fixture
def payments_off(as_released, monkeypatch):
    """The release, with payments, cancelling and reversing switched off as they were before they came on; it fails
    rather than let a test pass with them on."""
    for name in PAYMENTS:
        monkeypatch.setattr(REGISTRY.contracts[name], "planned", "Core")
    assert all(REGISTRY.contracts[n].planned == "Core" for n in PAYMENTS)


def only(engine, replies, said):
    c = TestClient(create_app(engine, ScriptedModel(list(replies)), embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    events = c.post(f"/v1/conversations/{cid}/messages", json={"text": said}).json()["events"]
    assert len(events) == 1, events
    return events[0]["text"]


def invented(engine):
    """The model answers in text with a figure no tool gave."""
    return only(engine, [Reply("Revenue in 2025 was 98,765.00 USD.")], "how are we doing this year?")


def test_a_tool_less_reply_to_a_planned_figure_the_owner_named_gets_the_planned_reply(engine, as_released):
    """The sentence the figure check used to see names the sales figure by a synonym: the catalogue answers first."""
    c = TestClient(create_app(engine, ScriptedModel([Reply("Revenue in 2025 was 98,765.00 USD.")]), embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    [e] = c.post(f"/v1/conversations/{cid}/messages", json={"text": "tell me my revenue in yr 2025"}).json()["events"]
    assert (e["type"], e.get("capability"), e.get("checked")) == ("answer", "planned.reports", "engine"), e


def gave_up(engine):
    """Every model request calls a tool that does not exist, until the assistant gives up."""
    return only(engine, [Reply("", [call("no_such_tool")]) for _ in range(MAX_MODEL_REQUESTS)], "do the thing")


def names_nothing_switched_off(text: str) -> None:
    for w in OFF_WORDS:
        assert not re.search(rf"\b{re.escape(w)}\b", text, re.I), (w, text)
    assert not re.search(r"\d", text), text


def test_an_invented_figure_is_replaced_by_what_is_switched_on(engine, as_released):
    text = invented(engine)
    assert text == f"I can only give figures that come from your books, and no tool gave me one for that. {ON_NOW}"
    names_nothing_switched_off(text)


def test_giving_up_suggests_only_what_is_switched_on(engine, as_released):
    text = gave_up(engine)
    assert text == f"I couldn't turn that into something I can do. {ON_NOW} Try asking for one of those."
    names_nothing_switched_off(text)


def test_both_texts_change_when_a_contract_is_switched_off(engine, payments_off, monkeypatch):
    monkeypatch.setattr(REGISTRY.contracts["submit_invoice"], "planned", "Core")
    for text in (invented(engine), gave_up(engine)):
        assert "Right now I can add customers, add items, and draft invoices." in text and "submit" not in text, text
        assert not re.search(r"\d", text), text


def test_with_sales_switched_off_the_replies_still_suggest_core_operations(engine, as_released):
    with engine.begin() as conn:
        disable_plugin(conn, "sales")
    assert gave_up(engine) == ("I couldn't turn that into something I can do. "
                               "Right now I can add customers and add items. Try asking for one of those.")
    assert invented(engine) == ("I can only give figures that come from your books, and no tool gave me one for that. "
                                "Right now I can add customers and add items.")
