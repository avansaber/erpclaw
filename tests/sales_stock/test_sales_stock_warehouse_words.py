"""A warehouse the owner names in chat is read from their words, never dropped: the right warehouse, its question
when the name is unknown or several hold enough, or a not-done reply; never the default warehouse without saying so.
The model is never offered the field; the assistant reads the phrase. The first three tests are the three sentences
that, before the reader, dropped the phrase or asked only about the customer."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from bookdiff import snapshot
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import models as m
from erpclaw_core.common import company_ctx, new_id
from erpclaw_stock import service as stock
from erpclaw_stock.service import Trusted


def _reply(customer: str) -> Reply:
    return Reply("", [call("create_invoice", customer=customer, items=[{"item": "copper wire", "quantity": 5}])])


@pytest.fixture
def stocked(engine):
    """The demo books' Copper Wire kept in stock: 20 in Main Store (the default), 10 in Raw Materials Store."""
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        main = stock.create_warehouse(ctx, "Main Store", default=True)
        raw = stock.create_warehouse(ctx, "Raw Materials Store")
        item = conn.execute(select(m.item.c.id).where(m.item.c.company_id == ctx.company_id,
                                                      m.item.c.name == "Copper Wire")).scalar_one()
        stock.make_stock_item(ctx, item, "moving_average")
        trusted = Trusted("the warehouse-words tests' starting stock")
        for wh, qty in ((main, "20"), (raw, "10")):
            stock.receive(ctx, f"pr-{new_id()[:8]}", item, wh, qty, "10.00", date(2026, 10, 1), trusted=trusted)
        trusted.verify(ctx)
    return engine


def _turns(engine, said: list[str], customer: str = "Acme Corporation") -> list[list[dict]]:
    a = Assistant(engine, ScriptedModel([_reply(customer)] * 8))
    return [a.turn("warehouse-words", s) for s in said]


def _card_lines(events: list[dict]) -> list[tuple[str, str]] | None:
    cards = [e for e in events if e.get("type") == "card"]
    return [tuple(x) for x in cards[0]["card"]["lines"]] if cards else None


def _never_the_default_quietly(events: list[dict]) -> None:
    lines = _card_lines(events)
    if lines is not None:
        assert not any(label == "From" and value.startswith("Main Store") for label, value in lines), lines


def test_probe_1_the_customer_is_confirmed_first_then_the_unknown_warehouse_is_asked(stocked):
    Assistant(stocked, ScriptedModel([]))
    before = snapshot(stocked)
    first, second = _turns(stocked, ["invoice Acme for 5 copper wire from the Pune warehouse", "yes"], "Acme")
    assert [e["code"] for e in first if e["type"] == "ask"] == ["customer_ambiguous"]  # records first
    asks = [e for e in second if e["type"] == "ask"]
    assert [e["code"] for e in asks] == ["warehouse_unknown"], second
    assert "Pune" in asks[0]["text"] and asks[0]["data"]["field"].endswith("items.0.warehouse")
    assert [c["label"] for c in asks[0]["data"]["choices"]] == ["Main Store (20 on hand)",
                                                               "Raw Materials Store (10 on hand)"]
    _never_the_default_quietly(second)
    assert snapshot(stocked) == before


def test_probe_2_an_unknown_warehouse_is_asked_never_dropped(stocked):
    [events] = _turns(stocked, ["invoice Acme Corporation for 5 copper wire from the Pune warehouse"])
    assert [e["code"] for e in events if e["type"] == "ask"] == ["warehouse_unknown"], events
    _never_the_default_quietly(events)


def test_probe_3_a_named_warehouse_is_the_one_the_card_ships_from(stocked):
    [events] = _turns(stocked, ["invoice Acme Corporation for 5 copper wire from the Raw Materials Store"])
    lines = _card_lines(events)
    assert lines is not None and ("From", "Raw Materials Store (10 on hand)") in lines, events


def test_the_warehouse_question_binds_its_answer(stocked):
    asked, answered = _turns(stocked, ["invoice Acme Corporation for 5 copper wire from the Pune warehouse",
                                       "Raw Materials Store"])
    assert [e["code"] for e in asked if e["type"] == "ask"] == ["warehouse_unknown"]
    lines = _card_lines(answered)
    assert lines is not None and ("From", "Raw Materials Store (10 on hand)") in lines, answered


def test_no_warehouse_named_takes_the_default_and_the_card_says_so(stocked):
    [events] = _turns(stocked, ["invoice Acme Corporation for 5 copper wire"])
    lines = _card_lines(events)
    assert lines is not None and ("From", "Main Store (20 on hand)") in lines, events  # named on the card
