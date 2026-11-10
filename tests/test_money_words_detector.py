"""Money instructions must not disappear, including in memo values."""
from decimal import Decimal

import pytest

from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core.common import company_ctx
from erpclaw_core.masters import create_item, create_party


# Each case names the detector mutation it catches in its id.
MONEY_PHRASES = [
    pytest.param("tax", "tax", id="singular-tax"),
    pytest.param("taxes", "tax", id="plural-tax"),
    pytest.param("with taxes included", "tax", id="plural-included-finding"),
    pytest.param("plus taxes", "tax", id="plural-plus"),
    pytest.param("including taxes", "tax", id="plural-including"),
    pytest.param("taxes extra", "tax", id="plural-extra"),
    pytest.param("sales taxes", "tax", id="plural-sales"),
    pytest.param("VAT", "tax", id="vat"),
    pytest.param("GST", "tax", id="gst"),
    pytest.param("at half off", "discount", id="half-off-finding"),
    pytest.param("20% off", "discount", id="percent-sign-off"),
    pytest.param("20 percent off", "discount", id="percent-word-off"),
    pytest.param("with a discount", "discount", id="singular-discount"),
    pytest.param("with the agreed discount", "discount", id="agreed-singular"),
    pytest.param("with the agreed discounts", "discount", id="agreed-plural-finding"),
    pytest.param("discounted", "discount", id="discounted"),
    pytest.param("less 20%", "discount", id="less-percent"),
    pytest.param("minus 20%", "discount", id="minus-percent"),
    pytest.param("a reduction of 20%", "discount", id="reduction-of"),
]


def invoice(engine, text, *, customer="Hooli", item="Copper Wire"):
    reply = Reply("", [call("create_invoice", customer=customer,
                           items=[{"item": item, "quantity": 5}])])
    return Assistant(engine, ScriptedModel([reply] * 3)).turn("words", text)


def assert_money_blocked(events, area):
    assert not any(e["type"] == "card" for e in events), events
    assert len(events) == 1, events
    event = events[0]
    assert event["type"] in {"ask", "refused", "answer"}, events
    if event["type"] == "answer":
        assert event["capability"] == {"tax": "planned.tax_engine", "discount": "planned.discounts"}[area]


@pytest.mark.parametrize("phrase,area", MONEY_PHRASES)
def test_requested_money_part_is_never_dropped(engine, phrase, area):
    events = invoice(engine, f"invoice Hooli for 5 copper wire {phrase}")
    assert_money_blocked(events, area)


@pytest.mark.parametrize("customer,item", [
    ("Taxi Co", "Copper Wire"),
    ("Hooli", "Half Moon Lamp"),
    ("Hooli", "Superdiscount Cable"),
    ("Model 20 Co", "Series 5 Lamp"),
])
def test_non_money_words_and_numbers_in_names_still_draft(engine, customer, item):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        if customer != "Hooli":
            create_party(ctx, "customer", customer)
        if item != "Copper Wire":
            create_item(ctx, item, Decimal("28"))
    events = invoice(engine, f"invoice {customer} for 5 {item}", customer=customer, item=item)
    assert [e["type"] for e in events] == ["card"], events


def test_plain_invoice_still_drafts(engine):
    events = invoice(engine, "invoice Hooli for 5 copper wire")
    assert [e["type"] for e in events] == ["card"], events
    assert ("Total", "140.00 USD") in [tuple(line) for line in events[0]["card"]["lines"]]


@pytest.mark.parametrize("memo,area", [
    ("memo: tax filing", "tax"), ("memo tax return", "tax"),
    ("note: tax filing", "tax"), ("reference tax return", "tax"),
    ('memo: "tax filing"', "tax"), ("memo: discount code ABC", "discount"),
    ('memo: "with taxes included"', "tax"),
])
def test_money_words_as_memo_values_are_refused(engine, memo, area):
    events = invoice(engine, f"invoice Hooli for 5 copper wire, {memo}")
    assert_planned_money(events, area)


@pytest.mark.parametrize("tail,area", [
    ("with tax, memo: filing", "tax"),
    ("with taxes included, memo: tax filing", "tax"),
    ("with a discount, memo: discount code ABC", "discount"),
    ('memo: "tax filing" and add taxes', "tax"),
    ("memo: tax filing and add taxes", "tax"),
    ("memo: tax filing, plus taxes", "tax"),
    ("memo: tax filing plus taxes", "tax"),
    ("memo: filing with taxes included", "tax"),
    ("memo: tax return with the agreed discounts", "tax"),
    ("memo: filing at half off", "discount"),
    ('memo: "tax return" with 20% off', "tax"),
    ("memo: tax return. Plus taxes", "tax"),
])
def test_money_words_outside_memo_still_block_card(engine, tail, area):
    events = invoice(engine, f"invoice Hooli for 5 copper wire, {tail}")
    assert_money_blocked(events, area)


def assert_planned_money(events, area):
    assert [event["type"] for event in events] == ["answer"], events
    assert events[0]["outcome"] == "c", events
    assert events[0]["capability"] == {"tax": "planned.tax_engine", "discount": "planned.discounts"}[area], events


def test_memo_value_is_refused_before_memo_choice(engine):
    reply = Reply("", [call("create_invoice", customer="Hooli",
                           items=[{"item": "Copper Wire", "quantity": 5}])])
    assistant = Assistant(engine, ScriptedModel([reply] * 3))
    events = assistant.turn("memo", "invoice Hooli for 5 copper wire, memo: tax filing")
    assert_planned_money(events, "tax")


def test_discount_inside_memo_refuses_without_drafting(engine):
    reply = Reply("", [call("create_invoice", customer="Hooli",
                           items=[{"item": "Copper Wire", "quantity": 5}])])
    assistant = Assistant(engine, ScriptedModel([reply] * 3))
    events = assistant.turn("memo-discount", "invoice Hooli for 5 copper wire, memo: filing and give 20% off")
    assert_planned_money(events, "discount")


def test_note_taxes_classified_as_instruction_refuses_without_drafting(engine):
    with engine.begin() as conn:
        create_item(company_ctx(conn), "Note Taxes", Decimal("28"))
    reply = Reply("", [call("create_invoice", customer="Hooli",
                           items=[{"item": "Note Taxes", "quantity": 5}])])
    assistant = Assistant(engine, ScriptedModel([reply] * 3))
    first = assistant.turn("note-taxes", "invoice Hooli for 5 Note Taxes")
    assert len(first) == 1 and first[0]["type"] == "ask", first
    assert first[0]["code"] == "money_name_ambiguous", first
    assert first[0]["options"] == ["Note Taxes", "no, I mean tax or a discount"], first
    events = assistant.turn("note-taxes", "no, I mean tax or a discount")
    assert_planned_money(events, "tax")
