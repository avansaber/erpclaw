"""Payments, cancelling and reversing, from the owner's words through the assistant and the API: every amount on the
card and in the result carries its value and the books' currency, and the result names what each amount is of."""
from __future__ import annotations

import re
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from erpclaw_api.app import create_app
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import models as m
from erpclaw_core.common import company_ctx
from erpclaw_sales import service as s
from erpclaw_sales import tables as t

TWO_PLACES = re.compile(r"^-?\d+\.\d{2}$")


def client(engine, *replies):
    c = TestClient(create_app(engine, ScriptedModel(list(replies)), embedded_owner=True))
    return c, c.post("/v1/conversations").json()["id"]


def say(c, cid, said):
    return c.post(f"/v1/conversations/{cid}/messages", json={"text": said}).json()["events"]


def approved(c, card):
    [result] = c.post(f"/v1/cards/{card['approval_id']}/approve", json={"digest": card["digest"]}).json()["events"]
    return result


def card_amounts_hold_their_currency(card, value: str) -> None:
    assert card["type"] == "card" and card["card"]["currency"] == "USD", card
    amounts = card["card"]["amounts"]
    assert amounts and all(a["currency"] == "USD" and TWO_PLACES.match(a["value"]) for a in amounts), amounts
    assert value in [a["value"] for a in amounts], (value, amounts)


def result_amounts(c, result) -> list[tuple[str, str, str, str]]:
    assert result["type"] == "result" and result["status"] == "committed" and result["currency"] == "USD", result
    got = [(a["what"], a["of"], a["value"], a["currency"]) for a in result["amounts"]]
    assert all(of and of in result["text"] for _, of, _, _ in got), (got, result["text"])
    rb = c.get(f"/v1/operations/{result['operation_key']}").json()  # the read-back carries the same amounts
    assert (rb["currency"], rb["amounts"]) == ("USD", result["amounts"])
    return got


def test_a_payment_s_card_and_result_amounts_carry_value_and_currency(engine):
    c, cid = client(engine, Reply("", [call("record_payment", customer="Pied Piper", amount=100,
                                            invoice="INV-2026-00006")]))
    [card] = say(c, cid, "Pied Piper paid 100 against INV-2026-00006")
    card_amounts_hold_their_currency(card, "100.00")
    got = result_amounts(c, approved(c, card))
    assert [(w, v, cur) for w, _, v, cur in got] == [("amount", "100.00", "USD"), ("applied", "100.00", "USD")], got
    assert got[0][1].startswith("PAY-") and got[1][1] == "INV-2026-00006", got


def test_a_payment_with_credit_left_names_the_credit(engine):
    c, cid = client(engine, Reply("", [call("record_payment", customer="Pied Piper", amount=5000,
                                            invoice="INV-2026-00006")]))
    [card] = say(c, cid, "Pied Piper paid 5000 against INV-2026-00006")
    card_amounts_hold_their_currency(card, "5000.00")
    got = result_amounts(c, approved(c, card))
    assert [w for w, *_ in got] == ["amount", "applied", "credit"], got
    amount, applied, credit = (Decimal(v) for _, _, v, _ in got)
    assert amount == applied + credit and got[2][1] == got[0][1], got  # the credit is the payment's own


def test_a_cancellation_s_card_and_result_amounts_carry_value_and_currency(engine):
    c, cid = client(engine, Reply("", [call("cancel_invoice", invoice="INV-2026-00003")]))
    [card] = say(c, cid, "cancel INV-2026-00003, it was a mistake")
    with engine.connect() as conn:
        total = conn.execute(select(t.invoice.c.total).where(t.invoice.c.number == "INV-2026-00003")).scalar_one()
    card_amounts_hold_their_currency(card, f"{Decimal(total):.2f}")
    got = result_amounts(c, approved(c, card))
    assert got == [("total", "INV-2026-00003", f"{Decimal(total):.2f}", "USD")], got


def test_a_reversal_s_card_and_result_amounts_carry_value_and_currency(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        cust = dict(conn.execute(select(m.party).where(m.party.c.name == "Pied Piper")).mappings().one())
        r = s.record_payment(ctx, s.plan_payment(ctx, cust, Decimal("100"), ctx.today, oldest_first=True))
        number = conn.execute(select(t.payment.c.number).where(t.payment.c.id == r["id"])).scalar_one()
    c, cid = client(engine, Reply("", [call("reverse_payment", payment=number)]))
    [card] = say(c, cid, f"reverse {number}, it bounced")
    card_amounts_hold_their_currency(card, "100.00")
    got = result_amounts(c, approved(c, card))
    assert got[0] == ("amount", number, "100.00", "USD"), got
    assert {w for w, *_ in got} <= {"amount", "reopened", "credit_removed"} and "reopened" in {w for w, *_ in got}, got
