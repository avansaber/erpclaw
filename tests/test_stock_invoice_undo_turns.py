"""Only the owner's invoice number may ground cancellation."""
import json

import pytest

from bookdiff import snapshot
from erpclaw_assistant import resolve, store
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import approvals, principals as pr
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.plugins import REGISTRY
from erpclaw_sales import CancelInvoice, preview_cancel


def test_cancel_by_number_previews_card(engine, as_released):
    number = "INV-2026-00003"
    other = "INV-2026-00005"
    before = snapshot(engine)
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        resolved = resolve.resolve_args(ctx, REGISTRY.contracts["cancel_invoice"], {"invoice": other},
                                        f"undo {number}, it was a mistake", resolve.Session())
        card = preview_cancel(ctx, CancelInvoice.model_validate(resolved.args)).card.as_dict()
    assert dict(card["lines"])["Invoice"] == number
    assert other not in str(card)
    assert snapshot(engine) == before


def test_cancel_without_grounding_asks_which(engine, as_released):
    before = snapshot(engine)
    with engine.connect() as conn, pytest.raises(Refusal) as caught:
        resolve.resolve_args(company_ctx(conn), REGISTRY.contracts["cancel_invoice"],
                             {"invoice": "INV-2026-00003"}, "undo that invoice", resolve.Session())
    assert (caught.value.code, str(caught.value)) == ("invoice_missing", "Which invoice?")
    assert snapshot(engine) == before


def test_undo_counts_as_write_intent():
    assert resolve.write_intents("undo that invoice") == 1
    assert resolve.write_intents("that invoice") == 0
    assert resolve.write_intents("undo invoice 3 and submit it") == 1


def test_second_distinct_write_after_undo_is_rejected(engine, as_released):
    a = Assistant(engine, ScriptedModel([Reply("", [
        call("create_invoice", customer="Hooli", items=[{"item": "Copper Wire", "quantity": 5}])])]))
    before = snapshot(engine)
    with engine.connect() as conn:
        card = preview_cancel(company_ctx(conn), CancelInvoice(invoice="INV-2026-00003")).card.as_dict()
    assert dict(card["lines"])["Invoice"] == "INV-2026-00003"
    assert resolve.write_intents("undo INV-2026-00003 and invoice Hooli for 5 Copper Wire") == 2
    ev = a.turn("two", "undo INV-2026-00003 and invoice Hooli for 5 Copper Wire")
    assert [e["type"] for e in ev] == ["ask"] and ev[0]["code"] == "changes_ambiguous", ev
    assert snapshot(engine) == before


def test_typed_approval_keeps_card_without_writing(engine, request):
    # The all-feature fixture allows a direct service card; chat never offers cancellation.
    a = Assistant(engine, ScriptedModel([Reply("Done.")]))
    with engine.connect() as conn:
        owner = pr.owner(conn)["id"]
    store.create_conversation("pending", owner)
    ownership = lambda cid: store.conversation_owner(cid) == owner
    card = approvals.propose(engine, REGISTRY.contracts["cancel_invoice"],
                             {"invoice": "INV-2026-00003"}, conversation_id="pending", ownership_check=ownership)
    request.getfixturevalue("as_released")
    before = snapshot(engine)
    out = a.graph.invoke({"conversation_id": "pending", "turn": [{"role": "user", "content": "approve"}],
                          "message_text": "approve", "request_text": "approve", "events": [],
                          "pending_contract": "cancel_invoice", "pending_card": card.approval_id,
                          "pending_args": json.dumps({"invoice": "INV-2026-00003"})})
    assert snapshot(engine) == before
    assert out["events"] == [{"type": "answer", "text": "Nothing has been saved yet. Approve the card to save it.",
                              "checked": "engine"}]
    assert approvals.pending_card(engine, "pending", ownership_check=ownership)["id"] == card.approval_id
    assert resolve.keeps_card("approve") and not resolve.keeps_card("approve but change invoice 5")
