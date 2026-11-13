"""Undo is an invoice cancellation verb, never a word counted inside a name."""
from decimal import Decimal

import pytest

from bookdiff import snapshot
from erpclaw_assistant import resolve
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core.common import company_ctx
from erpclaw_core.masters import create_item, create_party
from erpclaw_core.plugins import REGISTRY
from erpclaw_sales import CancelInvoice, preview_cancel


@pytest.mark.parametrize("customer,item", [
    ("Undo Corporation", "Copper Wire"),
    ("Acme Corporation", "Copper undo Wire"),
])
def test_undo_in_a_runtime_stored_name_is_one_invoice(engine, as_released, customer, item):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        if customer == "Undo Corporation":
            create_party(ctx, "customer", customer)
        else:
            create_item(ctx, item, Decimal("28.00"))
    text = f"invoice {customer} for 5 {item}"
    assistant = Assistant(engine, ScriptedModel([Reply("", [call(
        "create_invoice", customer=customer, items=[{"item": item, "quantity": 5}])])]))
    before = snapshot(engine)
    events = assistant.turn("stored-name", text)
    assert [e["type"] for e in events] == ["card"], events
    assert resolve.write_intents(text) == 1
    assert snapshot(engine)["sales_invoice"] == before["sales_invoice"]


@pytest.mark.parametrize("text,count", [
    ("undo invoice INV-2026-00003", 1),
    ("undo the last invoice", 1),
    ("please undo that invoice", 1),
    ("can you undo invoice INV-2026-00003", 1),
    ("undo INV-2026-00003 and invoice Acme for 5 Copper Wire", 2),
    ("invoice Acme for 5 Copper Wire and undo invoice INV-2026-00003", 2),
    ("show the undo history", 0),
    ("the undo button", 0),
    ("how does undo work", 0),
    ("invoice Acme for 5 Copper Wire with undo instructions", 1),
    ("invoice Acme for 5 Copper Wire; memo undo that invoice", 1),
])
def test_undo_counts_only_as_an_invoice_cancellation_verb(text, count):
    assert resolve.write_intents(text) == count


def test_undo_invoice_number_still_selects_cancellation(engine, as_released):
    number = "INV-2026-00003"
    before = snapshot(engine)
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        resolved = resolve.resolve_args(ctx, REGISTRY.contracts["cancel_invoice"], {"invoice": number},
                                        f"undo invoice {number}", resolve.Session())
        card = preview_cancel(ctx, CancelInvoice.model_validate(resolved.args)).card.as_dict()
    assert dict(card["lines"])["Invoice"] == number
    assert snapshot(engine) == before

