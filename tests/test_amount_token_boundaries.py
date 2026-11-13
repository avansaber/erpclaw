"""Currency runs own amounts only at token boundaries; punctuation and ordinals stay separate."""
from decimal import Decimal

import pytest

from erpclaw_assistant import resolve as rv
from erpclaw_core.common import company_ctx
from test_payment_resolve import pay, kinds, turns
from erpclaw_assistant.model import Reply, call, ToolCall


@pytest.mark.parametrize("token", [
    "0x123EUR500abc", "id9USD100", "a1EUR500", "EUR500abc", "USD100_id", "code$500x", "id€500x",
    "dead123EUR456beef", "aEUR500", "id100EUR7", "key100euros7", "abc100€xyz", "100USDabc",
])
def test_embedded_currency_run_creates_no_occurrence(engine, token):
    assert rv.figures(token) == []
    with engine.connect() as conn:
        assert rv.inventory(company_ctx(conn), token, []) == []
    ev = turns(engine, Reply("", [ToolCall("customer", "add_customer", {"name": token})])).turn("token", f"add customer {token}")
    assert kinds(ev) == ["card"], ev


@pytest.mark.parametrize("suffix", [", EUR", ". euros", "; €", ": EUR", ") EUR", "' EUR", '"] €'])
def test_punctuation_does_not_eat_a_foreign_mark(engine, suffix):
    ev = pay(engine, "Hooli paid 100" + suffix, amount=100)
    assert kinds(ev) == ["refused"] and ev[0]["code"] == "foreign_currency", ev


@pytest.mark.parametrize("text,value", [("EUR100", "100"), ("100EUR", "100"), ("€100", "100"),
                                        ("100€", "100"), ("100,thanks", "100"), ("5th", "5")])
def test_complete_amounts_and_ordinal_base_are_preserved(text, value):
    assert [n for n, _, _ in rv.figures(text)] == [Decimal(value)]


def test_ordinal_date_keeps_the_base_question(engine):
    ev = pay(engine, "Hooli paid 100 on the 5th", amount=100)
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "date_unknown", ev


def test_amount_followed_by_comma_is_still_unhandled_on_invoice(engine):
    ev = turns(engine, Reply("", [call("create_invoice", customer="Hooli", items=[{"item": "Copper Wire", "quantity": 5}])])).turn(
        "invoice", "invoice Hooli for 5 Copper Wire please $100,thanks")
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "figure_ambiguous", ev


@pytest.mark.parametrize("text,foreign", [
    ("100] , EUR", True), ("100 CA$", True), ("100CA$", True), ("100 US$", False), ("100US$", False),
    ("100 €5x", False), ("id€ 100", False), ("100 EUR5x", False),
])
def test_currency_attachment_uses_the_whole_mark_run(text, foreign):
    from erpclaw_core.common import Refusal
    if foreign:
        with pytest.raises(Refusal) as raised:
            rv.literal_figure(100, text, "USD", rv.Session(), "amount", ("How much was it?", []))
        assert raised.value.code == "foreign_currency"
    else:
        assert rv.literal_figure(100, text, "USD", rv.Session(), "amount", ("How much was it?", [])) == "100"


@pytest.mark.parametrize("suffix", ["XYZ$", "zz$", "QQQ$"])
def test_unrecognised_prefixed_symbol_is_not_a_currency_attachment(suffix):
    text = "100 " + suffix
    assert rv._marks_at(text, 0, 3) == []
    assert rv.literal_figure(100, text, "USD", rv.Session(), "amount", ("How much was it?", [])) == "100"


@pytest.mark.parametrize("closing", ["”", "’", "»", "）", "】"])
def test_currency_after_closing_unicode_punctuation_still_marks_amount(closing):
    from erpclaw_core.common import Refusal

    text = "100" + closing + " EUR"
    with pytest.raises(Refusal) as raised:
        rv.literal_figure(100, text, "USD", rv.Session(), "amount", ("How much was it?", []))
    assert raised.value.code == "foreign_currency"
