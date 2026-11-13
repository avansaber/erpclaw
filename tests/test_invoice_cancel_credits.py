"""Standing credits on any invoice line prevent a cancellation card."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from bookdiff import snapshot
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import approvals, models as m
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.masters import create_item
from erpclaw_core.plugins import REGISTRY
from erpclaw_stock import service as stock


def write(engine, name, **args):
    card = approvals.propose(engine, REGISTRY.contracts[name], args)
    outcome = approvals.approve(engine, card.approval_id, card.digest)
    assert outcome.status == "committed", outcome
    return outcome.data["number"]


@pytest.mark.parametrize("credited_item,everything", [
    ("Consulting", False),
    ("Copper Wire", False),
    (None, True),
])
def test_mixed_invoice_with_standing_credit_refuses_before_card(engine, credited_item, everything):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        create_item(ctx, "Consulting", Decimal("50.00"), unit="hour")
        item = conn.execute(select(m.item.c.id).where(m.item.c.company_id == ctx.company_id,
                            m.item.c.name == "Copper Wire")).scalar_one()
        warehouse = stock.create_warehouse(ctx, "Raw Materials Store", default=True)
        stock.make_stock_item(ctx, item, "moving_average", default_warehouse=warehouse)
        trusted = stock.Trusted("starting stock for cancellation planning")
        stock.receive(ctx, "starting-receipt", item, warehouse, "200", "28.00", date(2026, 10, 25), trusted=trusted)
        trusted.verify(ctx)
    number = write(engine, "create_invoice", customer="Acme Corporation", items=[
        {"item": "Copper Wire", "quantity": 5}, {"item": "Consulting", "quantity": 2}])
    credit = {"everything": True} if everything else {"items": [{"item": credited_item, "quantity": 1}]}
    write(engine, "create_credit_note", invoice=number, **credit)
    assistant = Assistant(engine, ScriptedModel([Reply("", [call("cancel_invoice", invoice=number)])]))
    before = snapshot(engine)
    with pytest.raises(Refusal) as caught:
        approvals.propose(engine, REGISTRY.contracts["cancel_invoice"], {"invoice": number})
    assert caught.value.code == "has_credit_notes"
    assert str(caught.value) == f"{number} has credit notes applied; it can't be cancelled."
    assert snapshot(engine) == before
    events = assistant.turn("credited-invoice", f"undo invoice {number}")
    assert [e["type"] for e in events] == ["refused"], events
    assert events[0]["code"] == "has_credit_notes"
    assert snapshot(engine) == before
