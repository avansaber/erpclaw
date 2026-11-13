"""Sales orders read their delivery date without the invoice-only date question."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core.common import company_ctx
from erpclaw_core.masters import create_item
from erpclaw_core.models import approval


@pytest.mark.parametrize("date_words, expected_date, shown_date", [
    ("tomorrow", "2026-10-27", "27 Oct 2026"),
    ("next Friday", "2026-10-30", "30 Oct 2026"),
    ("2026-11-12", "2026-11-12", "12 Nov 2026"),
    ("", "2026-10-26", "26 Oct 2026"),
], ids=["tomorrow", "next_friday", "explicit_date", "default_date"])
def test_sales_order_delivery_date_needs_no_invoice_date_choice(engine, date_words, expected_date, shown_date):
    write = call("create_sales_order", customer="Hooli",
                 items=[{"item": "Copper Wire", "quantity": 5}])
    assistant = Assistant(engine, ScriptedModel([Reply("", [write])]))
    events = assistant.turn("c1", f"Book a sales order for Hooli for 5 Copper Wire {date_words}".strip())

    assert not any(e["type"] == "ask" for e in events), events
    assert "Invoices are dated today" not in str(events), events
    assert not any(e["type"] == "not_done" and e.get("part") == "date" for e in events), events
    cards = [e for e in events if e["type"] == "card"]
    assert len(cards) == 1, events
    assert ["Delivery date", shown_date] in cards[0]["card"]["lines"], cards[0]
    with engine.connect() as conn:
        args = json.loads(conn.execute(select(approval.c.args_json).where(
            approval.c.id == cards[0]["approval_id"])).scalar_one())
    assert args["delivery_date"] == expected_date, args


@pytest.mark.parametrize("stored_name", ["Next Friday Pack", "Friday Pack", "Copper Wire Next Pack"])
def test_qualified_weekday_still_asks_when_a_stored_item_may_continue_the_name(engine, stored_name):
    with engine.begin() as conn:
        create_item(company_ctx(conn), stored_name, Decimal("1.00"))
    write = call("create_sales_order", customer="Hooli", items=[{"item": "Copper Wire", "quantity": 5}])
    assistant = Assistant(engine, ScriptedModel([Reply("", [write])]))
    events = assistant.turn("c1", "Book a sales order for Hooli for 5 Copper Wire next Friday")
    assert len(events) == 1 and events[0]["type"] == "ask", events
    assert events[0]["code"] == "item_ambiguous", events


def test_invoice_tomorrow_keeps_the_date_question_and_notice(engine):
    write = call("create_invoice", customer="Hooli", items=[{"item": "Copper Wire", "quantity": 5}])
    assistant = Assistant(engine, ScriptedModel([Reply("", [write]), Reply("", [write])]))
    events = assistant.turn("c1", "Invoice Hooli for 5 Copper Wire tomorrow")

    assert len(events) == 1 and events[0]["type"] == "ask", events
    question = events[0]
    notice = "Invoices are dated today; choosing an invoice date isn't available yet."
    assert question["code"] == "date_choice_missing", question
    assert question["text"] == "An invoice can't be dated in the future. Shall I date it today, or leave it?", question
    assert question["options"] == ["Date it today", "Leave it"], question
    assert [c["label"] for c in question["choices"]] == ["Date it today", "Leave it"], question

    events = assistant.turn("c1", "Date it today", choice=question["choices"][0]["code"])
    assert not any(e["type"] == "ask" for e in events), events
    assert len([e for e in events if e["type"] == "card"]) == 1, events
    assert [e for e in events if e["type"] == "not_done" and e.get("part") == "date"] == [
        {"type": "not_done", "part": "date", "capability": "planned.invoice_fields", "text": notice}
    ], events
