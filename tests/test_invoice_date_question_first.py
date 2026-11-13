"""Unread invoice dates are questioned before resolving the item's name."""

import pytest
from decimal import Decimal

from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core.common import company_ctx
from erpclaw_core.masters import create_item


@pytest.mark.parametrize("phrase", [
    "next November 3",
    "next 25 October",
    "last November 3",
    "this 25 October",
    "November 3 next",
    "coming November 3",
    "previous 25 October",
    "3 November coming",
    "next the 3rd",
    "last mid-November",
])
@pytest.mark.parametrize("position", ["before", "after"])
def test_unread_date_is_the_first_question(engine, phrase, position):
    write = call("create_invoice", customer="Hooli", items=[{"item": "copper wire", "quantity": 5}])
    assistant = Assistant(engine, ScriptedModel([Reply("", [write])]))
    request = "invoice Hooli for 5 copper wire"
    said = f"{phrase}, {request}" if position == "before" else f"{request} {phrase}"

    events = assistant.turn("invoice", said)
    assert len(events) == 1, events
    assert events[0]["type"] == "ask", events
    assert events[0]["code"] == "date_unknown", events
    assert events[0]["text"] == "Which date do you mean?", events
    assert events[0]["options"] == [], events


def test_calendar_words_in_a_stored_item_remain_the_items_name(engine):
    with engine.begin() as conn:
        create_item(company_ctx(conn), "Next November 3", Decimal("5"))
    write = call("create_invoice", customer="Hooli", items=[{"item": "Next November 3", "quantity": 5}])
    assistant = Assistant(engine, ScriptedModel([Reply("", [write])]))

    events = assistant.turn("name", "invoice Hooli for 5 Next November 3")
    assert not any(e.get("code") == "date_unknown" for e in events), events
    assert any(e["type"] == "card" for e in events), events


def test_an_answered_date_question_can_move_on(engine):
    write = call("create_invoice", customer="Hooli", items=[{"item": "copper wire", "quantity": 5}])
    assistant = Assistant(engine, ScriptedModel([Reply("", [write])] * 2))
    first = assistant.turn("answer", "invoice Hooli for 5 copper wire next November 3")
    assert first[0]["code"] == "date_unknown", first

    events = assistant.turn("answer", "3 November 2026")
    assert not any(e.get("code") == "date_unknown" for e in events), events
