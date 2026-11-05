"""The resolution step against the books: the sales contracts' marked fields resolved from the owner's words, then
proposed as cards (or asked, or refused as planned), with nothing written when no card is made."""
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from erpclaw_assistant.resolve import Planned, Session, resolve_args
from erpclaw_core import approvals
from erpclaw_core import models as m
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.masters import create_item, create_party
from erpclaw_core.plugins import REGISTRY
from erpclaw_sales import service as s
from erpclaw_sales import tables as t


def resolved(engine, op, args, text, session=None):
    with engine.connect() as conn:
        return resolve_args(company_ctx(conn), REGISTRY.contracts[op], args, text, session or Session())


def asked(engine, op, args, text, session=None) -> Refusal:
    before = count(engine)
    with pytest.raises(Refusal) as e:
        resolved(engine, op, args, text, session)
    assert count(engine) == before  # no card, nothing written
    return e.value


def card(engine, op, args):
    return approvals.propose(engine, REGISTRY.contracts[op], args)


def count(engine):
    with engine.connect() as conn:
        return tuple(conn.execute(select(func.count()).select_from(x)).scalar_one()
                     for x in (m.approval, m.posting, t.invoice, t.payment))


def add(engine, customers=(), items=()):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        for name in customers:
            create_party(ctx, "customer", name)
        for name, rate in items:
            create_item(ctx, name, Decimal(rate))


INV = "create_invoice"


@pytest.mark.parametrize("arg", ["Acme", "Corporation", "Acme Corporation"])
def test_the_owners_full_name_is_the_customer_on_the_card(engine, arg):
    r = resolved(engine, INV, {"customer": arg, "items": [{"item": "copper", "quantity": 2}]},
                 "invoice Acme Corporation for 2 copper wire")
    assert r.args["customer"] == "Acme Corporation" and r.args["items"] == [{"item": "Copper Wire", "quantity": "2"}]
    c = card(engine, INV, r.args)
    assert c.approval_id and r.not_done == []


def test_a_short_name_is_a_one_tap_confirm_and_the_answer_makes_the_card(engine):
    session = Session()
    q = asked(engine, INV, {"customer": "Acme", "items": [{"item": "Copper Wire", "quantity": 2}]},
              "invoice Acme for 2 copper wire", session)
    assert q.code == "customer_ambiguous" and q.options == ["Acme Corporation", "someone else"] and q.data["confirm"]
    assert session.answer(q.data["choices"][0]["code"])
    r = resolved(engine, INV, {"customer": "Acme", "items": [{"item": "Copper Wire", "quantity": 2}]},
                 "invoice Acme for 2 copper wire", session)
    assert r.args["customer"] == "Acme Corporation"
    card(engine, INV, r.args)


def test_omitted_fields_are_asked_one_at_a_time(engine):
    session = Session()
    q = asked(engine, INV, {"items": [{"item": "Copper Wire"}]}, "invoice them for copper wire", session)
    assert q.code == "customer_missing"
    session.bindings["customer"] = "Hooli"  # as the turn loop binds a typed answer
    q = asked(engine, INV, {"items": [{"item": "Copper Wire"}]}, "invoice them for copper wire", session)
    assert q.code == "quantity_missing" and q.message == "How many copper wires?"
    r = resolved(engine, INV, {"items": [{"item": "Copper Wire"}]}, "invoice them for 3 copper wire", session)
    assert r.args["customer"] == "Hooli" and r.args["items"][0]["quantity"] == "3"
    r = resolved(engine, INV, {}, "invoice Acme Corporation for two copper wire")  # omitted, but the owner wrote both
    assert r.args == {"customer": "Acme Corporation", "items": [{"item": "Copper Wire", "quantity": "2"}]}


@pytest.mark.parametrize("model_email", [True, False])
def test_emailing_is_said_beside_the_same_card_whatever_the_models_boolean(engine, model_email):
    r = resolved(engine, INV, {"customer": "Wayne Enterprises", "items": [{"item": "Circuit Board", "quantity": 2}],
                               "email": model_email}, "invoice Wayne Enterprises for 2 circuit boards and email it")
    assert "email" not in r.args and [e["part"] for e in r.not_done] == ["email"]
    c = card(engine, INV, r.args)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(m.outbox)).scalar_one() == 0
    assert c.card == card(engine, INV, r.args).card  # the event is not part of the card


def test_sales_tax_gets_the_planned_reply_and_writes_nothing(engine):
    before = count(engine)
    with pytest.raises(Planned) as e:
        resolved(engine, INV, {"customer": "Wayne Enterprises", "items": [{"item": "Circuit Board", "quantity": 3}]},
              "invoice Wayne Enterprises for 3 circuit boards and add 8% sales tax")
    assert e.value.route == "planned.tax_engine" and count(engine) == before


@pytest.mark.parametrize("said, route", [
    ("invoice Wayne Enterprises for 2 circuit boards at 40% off", "planned.discounts"),
    ("invoice Wayne Enterprises for 2 circuit boards due 15 November", "planned.payment_terms"),
])
def test_a_discount_or_a_due_date_gets_the_planned_reply_and_writes_nothing(engine, said, route):
    before = count(engine)
    with pytest.raises(Planned) as e:
        resolved(engine, INV, {"customer": "Wayne Enterprises", "items": [{"item": "Circuit Board", "quantity": 2}]},
                 said)
    assert e.value.route == route and count(engine) == before


@pytest.mark.parametrize("args, said", [
    ({"items": [{"item": "Circuit Board", "quantity": 2}]}, "invoice them for 2 circuit boards at 40% off"),
    ({"customer": "Acme", "items": [{"item": "Circuit Board", "quantity": 2}]},
     "invoice Acme for 2 circuit boards with 8% sales tax"),  # Acme is a confirm, the tax refusal comes first
    ({"customer": "Wayne", "items": [{"item": "Circuit Board", "quantity": 2}]},
     "invoice Wayne for 2 circuit boards, net 60"),
])
def test_the_planned_reply_comes_before_any_question(engine, args, said):
    with pytest.raises(Planned):
        resolved(engine, INV, args, said)


def test_a_tax_word_inside_a_resolved_name_makes_its_card(engine):
    add(engine, items=[("Tax Consulting", "150.00")])
    r = resolved(engine, INV, {"customer": "Wayne Enterprises", "items": [{"item": "Tax Consulting", "quantity": 2}]},
                 "invoice Wayne Enterprises for 2 tax consulting")
    card(engine, INV, r.args)


def test_selling_from_stock_gets_the_planned_reply_before_any_item_question(engine):
    with pytest.raises(Planned) as e:
        resolved(engine, INV, {"customer": "Wayne Enterprises", "items": [{"item": "Copy Paper", "quantity": 5}]},
              "invoice Wayne Enterprises for 5 copy paper from stock")
    assert e.value.route == "planned.stock" and e.value.message == "Selling from stock isn't available yet."


def test_a_stock_word_beside_a_service_item_is_never_refused(engine):
    add(engine, items=[("Training Day", "900.00")])
    args = {"customer": "Wayne Enterprises", "items": [{"item": "Training Day", "quantity": 2}]}
    for said in ("invoice Wayne Enterprises for 2 training days from stock",
                 "invoice Wayne Enterprises for 2 training days, in stock"):  # "in" bounds from table version 2
        card(engine, INV, resolved(engine, INV, args, said).args)


def test_names_that_normalise_alike_each_make_their_own_card(engine):
    add(engine, customers=["A&B", "AB"])
    for said in ("A&B", "AB"):
        r = resolved(engine, INV, {"customer": said, "items": [{"item": "Copper Wire", "quantity": 1}]},
                     f"invoice {said} for 1 copper wire")
        c = card(engine, INV, r.args)
        assert r.args["customer"] == said
        out = approvals.approve(engine, c.approval_id, c.digest)  # recomputed from the canonical name alone
        assert out.status == "committed", out.text
    q = asked(engine, INV, {"customer": "ab", "items": [{"item": "Copper Wire", "quantity": 1}]},
              "invoice ab for 1 copper wire")
    assert q.options == ["A&B", "AB"]


PAY = "record_payment"


def new_invoice(engine, customer="Hooli", qty="10") -> dict:
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        cust = dict(conn.execute(select(m.party).where(m.party.c.name == customer)).mappings().one())
        item = dict(conn.execute(select(m.item).where(m.item.c.name == "Copper Wire")).mappings().one())
        r = s.create_invoice(ctx, cust, [(item, Decimal(qty))], ctx.today)
        return dict(conn.execute(select(t.invoice).where(t.invoice.c.id == r["id"])).mappings().one())


def test_a_payment_with_the_optional_fields_omitted_makes_its_card(engine):
    r = resolved(engine, PAY, {"customer": "Hooli", "amount": Decimal("500")}, "Hooli paid 500")
    assert r.args == {"customer": "Hooli", "amount": "500"}  # today, and the plan's allocation
    card(engine, PAY, r.args)


def test_a_payment_date_comes_from_the_owners_one_date_phrase(engine):
    with engine.connect() as conn:
        today = company_ctx(conn).today
    r = resolved(engine, PAY, {"customer": "Hooli", "amount": Decimal("500")}, "Hooli paid 500 yesterday")
    assert r.args["date"] == (today - timedelta(days=1)).isoformat()
    q = asked(engine, PAY, {"customer": "Hooli", "amount": Decimal("500")}, "Hooli paid 500 yesterday or Monday")
    assert q.code == "date_ambiguous" and len(q.options) == 2


def test_the_payer_comes_from_the_owners_invoice_number(engine):
    inv = new_invoice(engine, qty="10")
    tail = str(int(inv["number"].rsplit("-", 1)[1]))
    r = resolved(engine, PAY, {"amount": Decimal("280"), "invoice": inv["number"]}, f"pay invoice {tail}, 280")
    assert r.args == {"customer": "Hooli", "amount": "280", "invoice": inv["number"]}
    card(engine, PAY, r.args)


def test_an_invoice_number_the_owner_did_not_write_is_never_used(engine):
    inv = new_invoice(engine, qty="10")
    r = resolved(engine, PAY, {"customer": "Hooli", "amount": Decimal("280"), "invoice": "INV-2026-09999"},
                 f"Hooli paid 280 on {inv['number']}")
    assert "invoice" not in r.args  # the model's number is not the owner's: left out


def test_an_amount_the_owner_did_not_write_asks_naming_the_open_invoices(engine):
    inv = new_invoice(engine, qty="10")
    q = asked(engine, PAY, {"customer": "Hooli", "amount": Decimal("280")}, "Hooli paid")
    assert q.code == "amount_missing" and q.message.startswith("How much did Hooli pay?") and inv["number"] in q.message


def test_a_payment_in_another_currency_is_refused(engine):
    q = asked(engine, PAY, {"customer": "Hooli", "amount": Decimal("500")}, "Hooli paid €500")
    assert q.code == "foreign_currency"


def test_a_proposal_through_the_api_is_not_resolved_from_words(engine):
    c = card(engine, INV, {"customer": "Acme Corporation", "items": [{"item": "Copper Wire", "quantity": 2}]})
    assert c.approval_id
    with pytest.raises(Refusal) as e:  # the contract's own resolver still asks on anything else
        card(engine, INV, {"customer": "Piper", "items": [{"item": "Copper Wire", "quantity": 2}]})
    assert e.value.code.endswith(("_ambiguous", "_unknown"))
