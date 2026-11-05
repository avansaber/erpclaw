"""A day word after a name, through the whole turn: it ends the name only where it ends the name's phrase, the
quantity is read against every stored item, and the day itself is still read, never swallowed by the name.
"""
from __future__ import annotations

from decimal import Decimal
import json

from sqlalchemy import select

import pytest

from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core.common import company_ctx
from erpclaw_core.masters import create_item, create_party
from erpclaw_core.models import approval


def inv(customer, *lines):
    return call("create_invoice", customer=customer, items=[{"item": i, "quantity": q} for i, q in lines])


def turn(engine, said, write):
    """One turn with the model replying with this one write."""
    return Assistant(engine, ScriptedModel([Reply("", [write])])).turn("c1", said)


def stored(engine, customers=(), items=()):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        for n in customers:
            create_party(ctx, "customer", n, None, None)
        for n in items:
            create_item(ctx, n, Decimal("1.00"))


def asks(events, code_end="_ambiguous"):
    return [e for e in events if e["type"] == "ask" and (e.get("code") or "").endswith(code_end)]


@pytest.mark.parametrize("customers, items, said, write", [
    (["Acme", "Monday Foods"], [], "invoice acme monday foods for 2 copper wire", inv("Acme", ("copper wire", 2))),
    ([], ["Sunday Roast"], "invoice Hooli for 5 copper wire sunday roast", inv("Hooli", ("copper wire", 5))),
    ([], ["Copper Wire Pack"], "invoice Hooli for 5 copper wire monday pack", inv("Hooli", ("copper wire", 5))),
    (["Acme"], [], "invoice Acme Sunday Ltd for 2 copper wire", inv("Acme", ("copper wire", 2))),
], ids=["a_customer_starting_with_the_day_word", "an_item_starting_with_the_day_word",
        "a_longer_item_going_on_after_the_day_word", "a_day_word_before_a_company_suffix"])
def test_a_day_word_the_name_may_go_on_through_asks_and_makes_no_card(engine, customers, items, said, write):
    stored(engine, customers, items)
    ev = turn(engine, said, write)
    assert asks(ev) and not [e for e in ev if e["type"] == "card"], ev


def test_each_lines_quantity_is_read_against_every_stored_item(engine):
    stored(engine, items=["Copper Wire Monday Pack"])
    ev = turn(engine, "invoice Hooli for 3 copper wire monday pack and 5 copper wire",
              inv("Hooli", ("Copper Wire Monday Pack", 3), ("Copper Wire", 5)))
    cards = [e for e in ev if e["type"] == "card"]
    assert len(cards) == 1, ev
    with engine.connect() as conn:
        args = json.loads(conn.execute(select(approval.c.args_json).where(
            approval.c.id == cards[0]["approval_id"])).scalar_one())
    assert [(row["item"], Decimal(str(row["quantity"]))) for row in args["items"]] == [
        ("Copper Wire Monday Pack", Decimal("3")), ("Copper Wire", Decimal("5")),
    ], args


@pytest.mark.parametrize("item", ["Copper Wire", "Copper Wire Monday Pack"])
def test_the_day_after_an_item_is_still_read_as_the_date(engine, item):
    """The name ends at "yesterday" and the day is the date rules' to read: no item confirm, and the invoice's date is
    asked about or noticed (invoices are dated today), never silently dropped into the name."""
    if item == "Copper Wire Monday Pack":
        stored(engine, items=[item])
    write = inv("Hooli", (item, 5))
    assistant = Assistant(engine, ScriptedModel([Reply("", [write]), Reply("", [write])]))
    ev = assistant.turn("c1", f"Invoice Hooli for 5 {item} yesterday")
    assert len(ev) == 1 and ev[0]["type"] == "ask" and ev[0]["code"] == "date_choice_missing", ev
    assert "Invoices are dated today" in ev[0]["text"], ev
    ev = assistant.turn("c1", "Date it today")
    assert not any(event["type"] == "ask" for event in ev), ev
    cards = [event for event in ev if event["type"] == "card"]
    assert len(cards) == 1, ev
    with engine.connect() as conn:
        args = json.loads(conn.execute(select(approval.c.args_json).where(
            approval.c.id == cards[0]["approval_id"])).scalar_one())
    assert [(row["item"], Decimal(str(row["quantity"]))) for row in args["items"]] == [
        (item, Decimal("5")),
    ], args
    dated = [event for event in ev if event["type"] == "not_done" and event.get("part") == "date"]
    assert len(dated) == 1 and dated[0]["capability"] == "planned.invoice_fields", ev
    assert "Invoices are dated today" in dated[0]["text"], dated
